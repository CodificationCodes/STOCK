"""Declarative base and shared column types.

Portability note: every column type used here maps cleanly onto both SQLite
and PostgreSQL, and no SQLite-specific SQL is used anywhere in the codebase,
so switching ``STOCKGAME_DATABASE_URL`` to ``postgresql+asyncpg://`` is a
configuration change rather than a port.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import DateTime, MetaData, TypeDecorator
from sqlalchemy.orm import DeclarativeBase

# Explicit constraint naming lets Alembic emit reversible ALTERs on any backend.
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class UTCDateTime(TypeDecorator):
    """Timezone-aware datetimes that survive SQLite's naive storage.

    SQLite drops tzinfo, so values come back naive and comparisons against
    aware datetimes blow up. This normalises to UTC on the way in and
    re-attaches UTC on the way out, on every backend.
    """

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: object) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    def process_result_value(self, value: datetime | None, dialect: object) -> datetime | None:
        if value is None:
            return None
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)
