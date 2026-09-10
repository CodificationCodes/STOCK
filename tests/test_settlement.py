"""Order settlement delay.

A market order does not fill when you click. It rests for
``order_delay_seconds`` and then fills at whatever the price is by then --
which is what stops a player reading a headline and front-running it.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from stockgame.server.db.models import Order, Portfolio
from stockgame.server.services.trading import OrderRequest
from stockgame.shared.enums import OrderStatus
from sqlalchemy import select

UTC = timezone.utc


@pytest.fixture
def settings(settings):
    settings.order_delay_seconds = 30.0
    return settings


async def place(game, user_id, side="buy", quantity=10, **extra):
    return await game.trading.place_order(
        user_id,
        OrderRequest.parse(
            {"symbol": "ACME", "side": side, "type": "market", "quantity": quantity, **extra}
        ),
    )


async def order_row(game, public_id) -> Order:
    async with game.db.session() as session:
        return await session.scalar(select(Order).where(Order.public_id == public_id))


async def cash_of(game, user_id) -> int:
    async with game.db.session() as session:
        portfolio = await session.scalar(select(Portfolio).where(Portfolio.user_id == user_id))
        return portfolio.cash_cents


async def settle(game, order_public_id):
    """Wind the order's clock back so the next tick treats it as due."""
    async with game.db.write_session() as session:
        order = await session.scalar(select(Order).where(Order.public_id == order_public_id))
        order.execute_after = datetime.now(UTC) - timedelta(seconds=1)
    return await game.trading.process_resting_orders()


class TestDelayedFills:
    @pytest.mark.asyncio
    async def test_a_market_order_does_not_fill_immediately(self, game, player):
        result = await place(game, player["user_id"])

        assert result["trades"] == []
        assert result["order"]["status"] == str(OrderStatus.PENDING.value)
        assert result["order"]["execute_after"] is not None

    @pytest.mark.asyncio
    async def test_it_fills_once_the_delay_has_elapsed(self, game, player):
        placed = await place(game, player["user_id"])

        updates = await settle(game, placed["order"]["id"])

        assert updates, "the settled order should have been picked up"
        row = await order_row(game, placed["order"]["id"])
        assert row.status == str(OrderStatus.FILLED.value)
        assert row.filled_quantity == 10

    @pytest.mark.asyncio
    async def test_a_pending_order_is_not_filled_early(self, game, player):
        await place(game, player["user_id"])

        assert await game.trading.process_resting_orders() == []

    @pytest.mark.asyncio
    async def test_it_fills_at_the_later_price_not_the_placement_price(self, game, player):
        placed = await place(game, player["user_id"])
        sim = game.engine.sims["ACME"]
        sim.price_cents = int(sim.price_cents * 1.5)  # the news lands after you click

        await settle(game, placed["order"]["id"])

        row = await order_row(game, placed["order"]["id"])
        assert row.avg_fill_cents >= sim.price_cents * 0.9


class TestReservations:
    @pytest.mark.asyncio
    async def test_cash_is_ring_fenced_while_settling(self, game, player):
        before = await cash_of(game, player["user_id"])

        await place(game, player["user_id"])

        assert await cash_of(game, player["user_id"]) < before

    @pytest.mark.asyncio
    async def test_cancelling_refunds_the_reservation(self, game, player):
        before = await cash_of(game, player["user_id"])
        placed = await place(game, player["user_id"])

        await game.trading.cancel_order(player["user_id"], placed["order"]["id"])

        assert await cash_of(game, player["user_id"]) == before

    @pytest.mark.asyncio
    async def test_unused_headroom_comes_back_after_the_fill(self, game, player):
        before = await cash_of(game, player["user_id"])
        placed = await place(game, player["user_id"])
        await settle(game, placed["order"]["id"])

        row = await order_row(game, placed["order"]["id"])
        spent = before - await cash_of(game, player["user_id"])
        # Only the traded value leaves the account, not the 5% buffer.
        assert spent == row.avg_fill_cents * row.filled_quantity
        assert row.reserved_cash_cents == 0

    @pytest.mark.asyncio
    async def test_a_runaway_price_rejects_rather_than_overdrawing(self, game, player):
        cash = await cash_of(game, player["user_id"])
        # Nearly all-in, leaving just enough room for the reservation buffer.
        quantity = int(cash * 0.9) // game.engine.sims["ACME"].price_cents
        placed = await place(game, player["user_id"], quantity=quantity)
        sim = game.engine.sims["ACME"]
        sim.price_cents = sim.price_cents * 10

        await settle(game, placed["order"]["id"])

        assert await cash_of(game, player["user_id"]) >= 0
        row = await order_row(game, placed["order"]["id"])
        assert row.status in (
            str(OrderStatus.REJECTED.value),
            str(OrderStatus.PARTIALLY_FILLED.value),
            str(OrderStatus.FILLED.value),
        )


class TestSells:
    @pytest.mark.asyncio
    async def test_shares_are_reserved_then_delivered(self, game, player):
        bought = await place(game, player["user_id"], quantity=20)
        await settle(game, bought["order"]["id"])

        sold = await place(game, player["user_id"], side="sell", quantity=20)
        assert sold["trades"] == []
        await settle(game, sold["order"]["id"])

        row = await order_row(game, sold["order"]["id"])
        assert row.status == str(OrderStatus.FILLED.value)
