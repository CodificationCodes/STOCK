"""The insider desk.

Buying a rumour must cost real money, must sometimes be worthless, and must
never tell the buyer which of those it was until the move has landed.
"""

from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from stockgame.server.core.errors import InsufficientFundsError, InvalidOrderError
from stockgame.server.db.models import InsiderTip, Portfolio
from stockgame.server.services.insider import move_for_fee

UTC = timezone.utc


async def cash_of(game, user_id) -> int:
    async with game.db.session() as session:
        portfolio = await session.scalar(select(Portfolio).where(Portfolio.user_id == user_id))
        return portfolio.cash_cents


async def make_due(game, public_id):
    async with game.db.write_session() as session:
        tip = await session.scalar(select(InsiderTip).where(InsiderTip.public_id == public_id))
        tip.applies_at = datetime.now(UTC) - timedelta(seconds=1)


class TestPricing:
    def test_a_bigger_cheque_buys_a_bigger_move(self, settings):
        small = move_for_fee(500_000, settings)  # $5k
        large = move_for_fee(20_000_000, settings)  # $200k
        assert large > small

    def test_the_payoff_saturates(self, settings):
        """Doubling the fee must not double the move, or the desk becomes a
        money printer for whoever has the deepest pockets."""
        base = move_for_fee(4_000_000, settings)
        doubled = move_for_fee(8_000_000, settings)
        assert doubled < base * 2

    def test_it_never_exceeds_the_configured_ceiling(self, settings):
        assert move_for_fee(10**12, settings) <= settings.insider_max_move_pct + 1e-9


class TestBuying:
    @pytest.mark.asyncio
    async def test_the_fee_leaves_the_account(self, game, player):
        before = await cash_of(game, player["user_id"])

        await game.insider.buy_tip(player["user_id"], 10_000.0)

        assert await cash_of(game, player["user_id"]) == before - 1_000_000

    @pytest.mark.asyncio
    async def test_it_names_a_stock_and_a_promise(self, game, player):
        tip = await game.insider.buy_tip(player["user_id"], 10_000.0)

        assert tip["symbol"] in game.engine.sims
        assert tip["promised_pct"] > 0
        assert tip["applied"] is False

    @pytest.mark.asyncio
    async def test_a_fresh_tip_does_not_reveal_whether_it_is_a_dud(self, game, player):
        tip = await game.insider.buy_tip(player["user_id"], 10_000.0)

        assert "genuine" not in tip
        assert "actual_pct" not in tip

    @pytest.mark.asyncio
    async def test_the_desk_refuses_pocket_change(self, game, player):
        with pytest.raises(InvalidOrderError):
            await game.insider.buy_tip(player["user_id"], 1.0)

    @pytest.mark.asyncio
    async def test_you_cannot_buy_what_you_cannot_afford(self, game, player):
        with pytest.raises(InsufficientFundsError):
            await game.insider.buy_tip(player["user_id"], 200_000.0)

    @pytest.mark.asyncio
    async def test_a_closed_desk_sells_nothing(self, game, player, settings):
        settings.insider_enabled = False
        with pytest.raises(InvalidOrderError):
            await game.insider.buy_tip(player["user_id"], 10_000.0)


class TestSettlement:
    @pytest.mark.asyncio
    async def test_nothing_moves_before_the_lead_time(self, game, player):
        tip = await game.insider.buy_tip(player["user_id"], 10_000.0)
        before = game.engine.sims[tip["symbol"]].price_cents

        assert await game.insider.apply_due_tips() == []
        assert game.engine.sims[tip["symbol"]].price_cents == before

    @pytest.mark.asyncio
    async def test_the_move_lands_once_due(self, game, player):
        game.insider.rng = random.Random(1)
        tip = await game.insider.buy_tip(player["user_id"], 10_000.0)
        sim = game.engine.sims[tip["symbol"]]
        before = sim.fair_value_cents
        await make_due(game, tip["id"])

        applied = await game.insider.apply_due_tips()

        assert len(applied) == 1
        assert sim.fair_value_cents > before

    @pytest.mark.asyncio
    async def test_settling_reveals_the_answer(self, game, player):
        tip = await game.insider.buy_tip(player["user_id"], 10_000.0)
        await make_due(game, tip["id"])

        applied = await game.insider.apply_due_tips()

        assert "genuine" in applied[0]
        assert "actual_pct" in applied[0]

    @pytest.mark.asyncio
    async def test_a_tip_only_fires_once(self, game, player):
        tip = await game.insider.buy_tip(player["user_id"], 10_000.0)
        await make_due(game, tip["id"])

        assert len(await game.insider.apply_due_tips()) == 1
        assert await game.insider.apply_due_tips() == []


class TestDuds:
    @pytest.mark.asyncio
    async def test_some_tips_are_duds(self, game, player, settings):
        """With a 100% dud rate the move must be a fraction of the promise --
        present, but nothing like what was advertised."""
        settings.insider_dud_chance = 1.0
        tip = await game.insider.buy_tip(player["user_id"], 10_000.0)
        await make_due(game, tip["id"])

        applied = await game.insider.apply_due_tips()

        assert applied[0]["genuine"] is False
        assert 0 < applied[0]["actual_pct"] < applied[0]["promised_pct"]

    @pytest.mark.asyncio
    async def test_a_genuine_tip_delivers_what_it_promised(self, game, player, settings):
        settings.insider_dud_chance = 0.0
        tip = await game.insider.buy_tip(player["user_id"], 10_000.0)
        await make_due(game, tip["id"])

        applied = await game.insider.apply_due_tips()

        assert applied[0]["genuine"] is True
        assert applied[0]["actual_pct"] == applied[0]["promised_pct"]


class TestHistory:
    @pytest.mark.asyncio
    async def test_tips_are_listed_newest_first_and_stay_sealed(self, game, player):
        await game.insider.buy_tip(player["user_id"], 5_000.0)
        await game.insider.buy_tip(player["user_id"], 6_000.0)

        tips = await game.insider.list_tips(player["user_id"])

        assert len(tips) == 2
        assert tips[0]["fee_cents"] == 600_000
        assert all("genuine" not in tip for tip in tips)

    @pytest.mark.asyncio
    async def test_you_only_see_your_own(self, game, player, rival):
        await game.insider.buy_tip(player["user_id"], 5_000.0)

        assert await game.insider.list_tips(rival["user_id"]) == []


class TestApi:
    @pytest.mark.asyncio
    async def test_buy_and_list_over_http(self, client, player):
        headers = {"Authorization": f"Bearer {player['token']}"}

        bought = await client.post("/api/insider", json={"amount": 10_000}, headers=headers)
        assert bought.status_code == 200
        assert bought.json()["promised_pct"] > 0

        listed = await client.get("/api/insider", headers=headers)
        assert [tip["id"] for tip in listed.json()["tips"]] == [bought.json()["id"]]

    @pytest.mark.asyncio
    async def test_a_tiny_amount_is_rejected(self, client, player):
        headers = {"Authorization": f"Bearer {player['token']}"}
        response = await client.post("/api/insider", json={"amount": 1}, headers=headers)
        assert response.status_code == 400

    @pytest.mark.asyncio
    async def test_it_needs_authentication(self, client):
        assert (await client.get("/api/insider")).status_code == 401
