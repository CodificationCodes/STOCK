"""Durability: the world must survive a server restart intact.

Each test builds a server, changes something, shuts it down completely and
brings a *new* server up against the same database file.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from sqlalchemy import func, select

from stockgame.server.app import create_app
from stockgame.server.config import Settings
from stockgame.server.db.models import Candle, Order, Stock, Trade, User
from stockgame.server.services.trading import OrderRequest
from tests.conftest import PASSWORD


class Restarter:
    """Starts and stops servers that share one database file."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def __aenter__(self):
        self.app = create_app(self.settings, run_loops=False)
        self._lifespan = self.app.router.lifespan_context(self.app)
        await self._lifespan.__aenter__()
        transport = httpx.ASGITransport(app=self.app)
        self.client = httpx.AsyncClient(transport=transport, base_url="http://testserver")
        return self

    async def __aexit__(self, *exc):
        await self.client.aclose()
        await self._lifespan.__aexit__(*exc)

    @property
    def game(self):
        return self.app.state.game


class TestRestart:
    async def test_accounts_and_cash_survive(self, settings):
        async with Restarter(settings) as first:
            response = await first.client.post(
                "/api/auth/register", json={"username": "persistent", "password": PASSWORD}
            )
            token = response.json()["token"]
            await first.game.trading.place_order(
                1,
                OrderRequest.parse(
                    {"symbol": "ACME", "side": "buy", "type": "market", "quantity": 60}
                ),
            )
            before = await first.game.portfolios.get_portfolio(1)

        async with Restarter(settings) as second:
            # The session token still works: it is in the database, not memory.
            me = await second.client.get(
                "/api/auth/me", headers={"Authorization": f"Bearer {token}"}
            )
            assert me.status_code == 200
            assert me.json()["username"] == "persistent"

            after = await second.game.portfolios.get_portfolio(1)
            assert after["cash_cents"] == before["cash_cents"]
            assert after["positions"][0]["quantity"] == 60
            assert (
                after["positions"][0]["cost_basis_cents"]
                == before["positions"][0]["cost_basis_cents"]
            )

    async def test_the_market_is_not_reseeded_on_restart(self, settings):
        async with Restarter(settings) as first:
            prices = first.game.engine.prices()
            day = first.game.engine.day_index
            for _ in range(first.settings.ticks_per_candle):
                await first.game.tick_once()
            await first.game.engine.flush()
            expected = first.game.engine.prices()

        async with Restarter(settings) as second:
            assert second.game.engine.day_index == day
            # Prices resume from the last flush, not from the genesis values.
            assert second.game.engine.prices() == expected
            assert second.game.engine.prices() != prices

        async with Restarter(settings) as third:
            async with third.game.db.session() as session:
                stocks = await session.scalar(select(func.count()).select_from(Stock))
            assert stocks == 47  # not 94: seeding ran exactly once

    async def test_trade_history_is_append_only(self, settings):
        async with Restarter(settings) as first:
            await first.client.post(
                "/api/auth/register", json={"username": "historian", "password": PASSWORD}
            )
            for _ in range(3):
                await first.game.trading.place_order(
                    1,
                    OrderRequest.parse(
                        {"symbol": "BYTE", "side": "buy", "type": "market", "quantity": 5}
                    ),
                )

        async with Restarter(settings) as second:
            trades = await second.game.trading.list_trades(1)
            assert len(trades) == 3
            async with second.game.db.session() as session:
                stored = await session.scalar(select(func.count()).select_from(Trade))
            assert stored == 3

    async def test_resting_orders_survive_and_still_fill(self, settings):
        async with Restarter(settings) as first:
            await first.client.post(
                "/api/auth/register", json={"username": "patient", "password": PASSWORD}
            )
            price = first.game.engine.price_of("ACME")
            result = await first.game.trading.place_order(
                1,
                OrderRequest.parse(
                    {
                        "symbol": "ACME",
                        "side": "buy",
                        "type": "limit",
                        "quantity": 10,
                        "limit_price": price / 100 * 0.5,
                    }
                ),
            )
            order_id = result["order"]["id"]
            assert result["order"]["status"] == "pending"

        async with Restarter(settings) as second:
            open_orders = await second.game.trading.list_orders(1, open_only=True)
            assert [o["id"] for o in open_orders] == [order_id]

            # Crash the price to the limit; the resting order must still work.
            second.game.engine.sims["ACME"].price_cents = int(
                second.game.engine.price_of("ACME") * 0.4
            )
            await second.game.trading.process_resting_orders()
            portfolio = await second.game.portfolios.get_portfolio(1)
            assert portfolio["positions"][0]["quantity"] == 10

    async def test_watchlist_and_achievements_persist(self, settings):
        async with Restarter(settings) as first:
            await first.client.post(
                "/api/auth/register", json={"username": "collector", "password": PASSWORD}
            )
            await first.game.portfolios.add_to_watchlist(1, "QNTM")
            await first.game.trading.place_order(
                1,
                OrderRequest.parse(
                    {"symbol": "ACME", "side": "buy", "type": "market", "quantity": 1}
                ),
            )

        async with Restarter(settings) as second:
            watchlist = await second.game.portfolios.get_watchlist(1)
            assert "QNTM" in {row["symbol"] for row in watchlist}
            unlocked = {
                badge["code"]
                for badge in await second.game.achievements.list_for(1)
                if badge["unlocked"]
            }
            assert "first_trade" in unlocked

    async def test_price_history_accumulates_across_restarts(self, settings):
        async with Restarter(settings) as first:
            async with first.game.db.session() as session:
                before = await session.scalar(select(func.count()).select_from(Candle))
            for _ in range(first.settings.ticks_per_candle * 2):
                await first.game.tick_once()

        async with Restarter(settings) as second:
            async with second.game.db.session() as session:
                after = await session.scalar(select(func.count()).select_from(Candle))
            assert after > before


class TestSchema:
    async def test_every_table_is_created(self, game):
        from stockgame.server.db.base import Base

        async with game.db.session() as session:
            for table in Base.metadata.sorted_tables:
                # Raises if the table does not exist.
                await session.execute(select(func.count()).select_from(table))

    async def test_money_columns_are_integers(self, game, player, buy):
        """Cents must never be stored as floats."""
        await buy(player["user_id"], "ACME", 7)
        async with game.db.session() as session:
            trade = await session.scalar(select(Trade))
            order = await session.scalar(select(Order))
        assert isinstance(trade.price_cents, int)
        assert isinstance(order.avg_fill_cents, int)

    async def test_cash_has_a_non_negative_constraint(self, game, player):
        from sqlalchemy.exc import IntegrityError

        from stockgame.server.db.models import Portfolio

        with pytest.raises(IntegrityError):
            async with game.db.write_session() as session:
                portfolio = await session.scalar(select(Portfolio))
                portfolio.cash_cents = -1

    async def test_usernames_are_unique_at_the_database_level(self, game, player):
        from sqlalchemy.exc import IntegrityError

        with pytest.raises(IntegrityError):
            async with game.db.write_session() as session:
                session.add(
                    User(
                        username="Alice2",
                        username_lower="alice",
                        password_hash="x",
                        is_admin=False,
                    )
                )

    async def test_datetimes_come_back_timezone_aware(self, game, player, buy):
        await buy(player["user_id"], "ACME", 1)
        async with game.db.session() as session:
            trade = await session.scalar(select(Trade))
        assert trade.created_at.tzinfo is not None

    async def test_the_database_file_is_actually_written(self, settings):
        async with Restarter(settings):
            pass
        path = Path(settings.database_url.split("///")[-1])
        assert path.exists() and path.stat().st_size > 0
