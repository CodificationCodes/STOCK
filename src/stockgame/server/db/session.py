"""Async engine + session management.

SQLite is a single-writer database, so all write transactions are funnelled
through :func:`write_session`, which holds a process-wide lock. On PostgreSQL
that lock is unnecessary but harmless; :data:`Database.needs_write_lock`
turns it off automatically for non-SQLite URLs.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from pathlib import Path

from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from stockgame.server.config import Settings
from stockgame.server.db.base import Base


class Database:
    """Owns the engine and hands out sessions."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.url = settings.database_url
        self.needs_write_lock = self.url.startswith("sqlite")
        self._write_lock = asyncio.Lock()
        if self.needs_write_lock:
            self._prepare_sqlite_path()
        self.engine: AsyncEngine = create_async_engine(
            self.url,
            echo=settings.debug and False,
            future=True,
            pool_pre_ping=not self.needs_write_lock,
            # SQLite's async driver serialises anyway; a bigger pool just
            # multiplies "database is locked" retries.
            **({} if self.needs_write_lock else {"pool_size": 10, "max_overflow": 20}),
        )
        if self.needs_write_lock:
            self._configure_sqlite_pragmas()
        self.session_factory = async_sessionmaker(
            self.engine, expire_on_commit=False, class_=AsyncSession
        )

    def _prepare_sqlite_path(self) -> None:
        _, _, path = self.url.partition(":///")
        if path and path != ":memory:":
            Path(path).expanduser().parent.mkdir(parents=True, exist_ok=True)

    def _configure_sqlite_pragmas(self) -> None:
        @event.listens_for(self.engine.sync_engine, "connect")
        def _set_pragmas(dbapi_conn, _record):  # type: ignore[no-untyped-def]
            cursor = dbapi_conn.cursor()
            # WAL lets readers proceed during a write; the busy timeout keeps
            # brief contention from surfacing as an error.
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA synchronous=NORMAL")
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA busy_timeout=5000")
            cursor.close()

    @contextlib.asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        """Read-oriented session. Does not take the write lock."""
        async with self.session_factory() as session:
            yield session

    @contextlib.asynccontextmanager
    async def write_session(self) -> AsyncIterator[AsyncSession]:
        """Serialised read-write session, committed on clean exit."""
        if self.needs_write_lock:
            async with self._write_lock, self.session_factory() as session:
                try:
                    yield session
                    await session.commit()
                except Exception:
                    await session.rollback()
                    raise
        else:
            async with self.session_factory() as session:
                try:
                    yield session
                    await session.commit()
                except Exception:
                    await session.rollback()
                    raise

    async def create_all(self) -> None:
        """Create any missing tables (dev/test path; production uses Alembic)."""
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    async def healthcheck(self) -> bool:
        try:
            async with self.session() as session:
                await session.execute(text("SELECT 1"))
            return True
        except Exception:
            return False

    async def dispose(self) -> None:
        await self.engine.dispose()
