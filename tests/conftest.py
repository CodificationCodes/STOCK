"""Shared fixtures.

Every test gets its own throwaway SQLite file and its own fully-wired
:class:`GameServer`. The background loops are *not* started -- tests advance
the market explicitly with ``game.tick_once()`` so behaviour is deterministic
and nothing depends on wall-clock timing.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
import pytest_asyncio

from stockgame.server.app import create_app
from stockgame.server.config import Settings
from stockgame.server.core.game import GameServer
from stockgame.server.services.trading import OrderRequest

PASSWORD = "correct-horse-7"


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'test.sqlite3'}",
        data_dir=tmp_path,
        secret_key="test-secret-key-not-for-production",
        # A short history keeps seeding fast while still exercising the same
        # code path that generates a year of candles in production.
        history_days=20,
        tick_seconds=1.0,
        day_seconds=30,
        ticks_per_candle=3,
        market_seed=1234,
        # Argon2 at production cost would make the auth tests take minutes.
        argon2_time_cost=1,
        argon2_memory_cost=1024,
        argon2_parallelism=1,
        auth_rate_limit=1000,
        order_rate_limit=1000,
        leaderboard_interval_seconds=3600,
        starting_cash=100_000.0,
        # Always open, so the suite does not depend on when it is run.
        # tests/test_hours.py covers the real schedule.
        market_open_time="00:00",
        market_close_time="23:59",
        market_weekdays_only=False,
    )


@pytest_asyncio.fixture
async def app(settings: Settings) -> AsyncIterator:
    application = create_app(settings, run_loops=False)
    async with application.router.lifespan_context(application):
        yield application


@pytest_asyncio.fixture
async def client(app) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as session:
        yield session


@pytest.fixture
def game(app) -> GameServer:
    return app.state.game


@pytest_asyncio.fixture
async def player(client: httpx.AsyncClient) -> dict:
    """A registered account plus its auth header."""
    response = await client.post(
        "/api/auth/register", json={"username": "alice", "password": PASSWORD}
    )
    assert response.status_code == 200, response.text
    data = response.json()
    return {
        "username": data["username"],
        "token": data["token"],
        "headers": {"Authorization": f"Bearer {data['token']}"},
        "user_id": 1,
    }


@pytest_asyncio.fixture
async def rival(client: httpx.AsyncClient) -> dict:
    """A second account, for multiplayer and isolation tests."""
    response = await client.post(
        "/api/auth/register", json={"username": "bob", "password": PASSWORD}
    )
    assert response.status_code == 200, response.text
    data = response.json()
    return {
        "username": data["username"],
        "token": data["token"],
        "headers": {"Authorization": f"Bearer {data['token']}"},
        "user_id": 2,
    }


@pytest.fixture
def buy(game: GameServer):
    """``await buy(user_id, "ACME", 100)`` -> the order/trades payload."""

    async def _buy(user_id: int, symbol: str = "ACME", quantity: int = 10, **extra):
        return await game.trading.place_order(
            user_id,
            OrderRequest.parse(
                {"symbol": symbol, "side": "buy", "type": "market", "quantity": quantity, **extra}
            ),
        )

    return _buy


@pytest.fixture
def sell(game: GameServer):
    async def _sell(user_id: int, symbol: str = "ACME", quantity: int = 10, **extra):
        return await game.trading.place_order(
            user_id,
            OrderRequest.parse(
                {"symbol": symbol, "side": "sell", "type": "market", "quantity": quantity, **extra}
            ),
        )

    return _sell
