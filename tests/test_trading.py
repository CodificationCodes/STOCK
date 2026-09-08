"""Order placement, execution, limits and the invariants that protect money."""

from __future__ import annotations

import pytest

from stockgame.server.core.errors import (
    InsufficientFundsError,
    InsufficientSharesError,
    MarketClosedError,
    NotFoundError,
)
from stockgame.server.services.trading import OrderRequest
from stockgame.shared.enums import MarketStatus, OrderSide
from stockgame.shared.validation import ValidationError


class TestMarketOrders:
    async def test_buy_fills_and_debits_cash(self, game, player, buy):
        before = await game.portfolios.get_portfolio(player["user_id"])
        result = await buy(player["user_id"], "ACME", 100)

        order, trade = result["order"], result["trades"][0]
        assert order["status"] == "filled"
        assert order["filled_quantity"] == 100
        assert trade["quantity"] == 100

        after = await game.portfolios.get_portfolio(player["user_id"])
        spent = before["cash_cents"] - after["cash_cents"]
        assert spent == trade["quantity"] * trade["price_cents"]
        assert after["positions"][0]["symbol"] == "ACME"
        assert after["positions"][0]["quantity"] == 100

    async def test_sell_credits_cash_and_closes_the_position(self, game, player, buy, sell):
        await buy(player["user_id"], "ACME", 50)
        mid = await game.portfolios.get_portfolio(player["user_id"])
        result = await sell(player["user_id"], "ACME", 50)

        after = await game.portfolios.get_portfolio(player["user_id"])
        proceeds = after["cash_cents"] - mid["cash_cents"]
        assert proceeds == 50 * result["trades"][0]["price_cents"]
        assert after["positions"] == []

    async def test_partial_sell_keeps_the_remainder(self, game, player, buy, sell):
        await buy(player["user_id"], "ACME", 100)
        await sell(player["user_id"], "ACME", 40)
        after = await game.portfolios.get_portfolio(player["user_id"])
        assert after["positions"][0]["quantity"] == 60

    async def test_buying_more_costs_more_per_share(self, game, player):
        """Slippage must scale with size, or large orders would be free."""
        small = game.trading.venue.quote("ACME", OrderSide.BUY, 10)
        large = game.trading.venue.quote("ACME", OrderSide.BUY, 100_000)
        assert large > small

    async def test_selling_size_receives_less_per_share(self, game, player):
        small = game.trading.venue.quote("ACME", OrderSide.SELL, 10)
        large = game.trading.venue.quote("ACME", OrderSide.SELL, 100_000)
        assert large < small

    async def test_buy_then_sell_immediately_loses_the_spread(self, game, player, buy, sell):
        start = await game.portfolios.get_portfolio(player["user_id"])
        await buy(player["user_id"], "ACME", 100)
        await sell(player["user_id"], "ACME", 100)
        end = await game.portfolios.get_portfolio(player["user_id"])
        # Round-tripping instantly must cost money; otherwise the spread is
        # not being applied and the game is trivially exploitable.
        assert end["total_value_cents"] < start["total_value_cents"]


class TestInsufficientFunds:
    async def test_cannot_buy_beyond_cash(self, game, player, buy):
        with pytest.raises(InsufficientFundsError):
            await buy(player["user_id"], "ACME", 1_000_000)

    async def test_rejected_order_leaves_cash_untouched(self, game, player, buy):
        before = await game.portfolios.get_portfolio(player["user_id"])
        with pytest.raises(InsufficientFundsError):
            await buy(player["user_id"], "ACME", 900_000)
        after = await game.portfolios.get_portfolio(player["user_id"])
        assert after["cash_cents"] == before["cash_cents"]

    async def test_cash_never_goes_negative_across_many_buys(self, game, player, buy):
        """Spend until refused, then assert the balance is still sane."""
        for _ in range(40):
            try:
                await buy(player["user_id"], "ACME", 50)
            except InsufficientFundsError:
                break
        portfolio = await game.portfolios.get_portfolio(player["user_id"])
        assert portfolio["cash_cents"] >= 0


class TestInsufficientShares:
    async def test_cannot_sell_what_you_do_not_own(self, game, player, sell):
        with pytest.raises(InsufficientSharesError):
            await sell(player["user_id"], "ACME", 1)

    async def test_cannot_sell_more_than_held(self, game, player, buy, sell):
        await buy(player["user_id"], "ACME", 10)
        with pytest.raises(InsufficientSharesError):
            await sell(player["user_id"], "ACME", 11)

    async def test_short_selling_is_not_possible(self, game, player, buy, sell):
        await buy(player["user_id"], "ACME", 10)
        await sell(player["user_id"], "ACME", 10)
        with pytest.raises(InsufficientSharesError):
            await sell(player["user_id"], "ACME", 1)


class TestLimitOrders:
    async def test_unmarketable_buy_rests_and_reserves_cash(self, game, player):
        price = game.engine.price_of("ACME")
        before = await game.portfolios.get_portfolio(player["user_id"])
        result = await game.trading.place_order(
            player["user_id"],
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
        assert result["order"]["status"] == "pending"
        assert result["trades"] == []

        after = await game.portfolios.get_portfolio(player["user_id"])
        # The cash is ring-fenced immediately so it cannot back a second order.
        assert after["cash_cents"] < before["cash_cents"]

    async def test_marketable_limit_fills_straight_away(self, game, player):
        price = game.engine.price_of("ACME")
        result = await game.trading.place_order(
            player["user_id"],
            OrderRequest.parse(
                {
                    "symbol": "ACME",
                    "side": "buy",
                    "type": "limit",
                    "quantity": 5,
                    "limit_price": price / 100 * 1.5,
                }
            ),
        )
        assert result["order"]["filled_quantity"] > 0
        # Never worse than the limit.
        assert result["trades"][0]["price_cents"] <= int(price * 1.5)

    async def test_resting_buy_fills_when_the_price_falls_to_it(self, game, player):
        price = game.engine.price_of("ACME")
        await game.trading.place_order(
            player["user_id"],
            OrderRequest.parse(
                {
                    "symbol": "ACME",
                    "side": "buy",
                    "type": "limit",
                    "quantity": 10,
                    "limit_price": price / 100 * 0.90,
                }
            ),
        )
        # Move the market down to the limit, then let the engine sweep.
        game.engine.sims["ACME"].price_cents = int(price * 0.85)
        await game.trading.process_resting_orders()

        orders = await game.trading.list_orders(player["user_id"])
        assert orders[0]["filled_quantity"] > 0
        assert orders[0]["avg_fill_cents"] <= int(price * 0.90)

    async def test_resting_sell_fills_when_the_price_rises_to_it(self, game, player, buy):
        await buy(player["user_id"], "ACME", 20)
        price = game.engine.price_of("ACME")
        await game.trading.place_order(
            player["user_id"],
            OrderRequest.parse(
                {
                    "symbol": "ACME",
                    "side": "sell",
                    "type": "limit",
                    "quantity": 20,
                    "limit_price": price / 100 * 1.10,
                }
            ),
        )
        game.engine.sims["ACME"].price_cents = int(price * 1.20)
        await game.trading.process_resting_orders()

        open_orders = await game.trading.list_orders(player["user_id"], open_only=True)
        assert not any(o["side"] == "sell" for o in open_orders)

    async def test_reserved_shares_cannot_be_sold_twice(self, game, player, buy, sell):
        await buy(player["user_id"], "ACME", 20)
        price = game.engine.price_of("ACME")
        await game.trading.place_order(
            player["user_id"],
            OrderRequest.parse(
                {
                    "symbol": "ACME",
                    "side": "sell",
                    "type": "limit",
                    "quantity": 20,
                    "limit_price": price / 100 * 2,
                }
            ),
        )
        with pytest.raises(InsufficientSharesError):
            await sell(player["user_id"], "ACME", 20)

    async def test_cancelling_refunds_reserved_cash(self, game, player):
        price = game.engine.price_of("ACME")
        before = await game.portfolios.get_portfolio(player["user_id"])
        result = await game.trading.place_order(
            player["user_id"],
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
        cancelled = await game.trading.cancel_order(player["user_id"], result["order"]["id"])
        assert cancelled["status"] == "cancelled"

        after = await game.portfolios.get_portfolio(player["user_id"])
        assert after["cash_cents"] == before["cash_cents"]

    async def test_cancelling_releases_reserved_shares(self, game, player, buy, sell):
        await buy(player["user_id"], "ACME", 20)
        price = game.engine.price_of("ACME")
        result = await game.trading.place_order(
            player["user_id"],
            OrderRequest.parse(
                {
                    "symbol": "ACME",
                    "side": "sell",
                    "type": "limit",
                    "quantity": 20,
                    "limit_price": price / 100 * 2,
                }
            ),
        )
        await game.trading.cancel_order(player["user_id"], result["order"]["id"])
        await sell(player["user_id"], "ACME", 20)  # must now succeed

    async def test_cannot_cancel_another_players_order(self, game, player, rival):
        price = game.engine.price_of("ACME")
        result = await game.trading.place_order(
            player["user_id"],
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
        with pytest.raises(NotFoundError):
            await game.trading.cancel_order(rival["user_id"], result["order"]["id"])

    async def test_cannot_cancel_a_filled_order(self, game, player, buy):
        result = await buy(player["user_id"], "ACME", 5)
        from stockgame.server.core.errors import InvalidOrderError

        with pytest.raises(InvalidOrderError):
            await game.trading.cancel_order(player["user_id"], result["order"]["id"])


class TestOrderValidation:
    @pytest.mark.parametrize(
        "payload",
        [
            {"symbol": "ACME", "side": "buy", "type": "market", "quantity": 0},
            {"symbol": "ACME", "side": "buy", "type": "market", "quantity": -5},
            {"symbol": "ACME", "side": "buy", "type": "market", "quantity": "abc"},
            {"symbol": "ACME", "side": "sideways", "type": "market", "quantity": 1},
            {"symbol": "ACME", "side": "buy", "type": "telepathic", "quantity": 1},
            {"symbol": "!!", "side": "buy", "type": "market", "quantity": 1},
            {"symbol": "ACME", "side": "buy", "type": "limit", "quantity": 1},
            {"symbol": "ACME", "side": "buy", "type": "limit", "quantity": 1, "limit_price": -3},
            {"symbol": "ACME", "side": "buy", "type": "market", "quantity": 10_000_000},
        ],
    )
    def test_bad_requests_are_refused_at_parse_time(self, payload):
        with pytest.raises(ValidationError):
            OrderRequest.parse(payload)

    async def test_unknown_symbol_is_rejected(self, game, player):
        with pytest.raises(NotFoundError):
            await game.trading.place_order(
                player["user_id"],
                OrderRequest.parse(
                    {"symbol": "ZZZZ", "side": "buy", "type": "market", "quantity": 1}
                ),
            )

    async def test_orders_refused_while_the_market_is_closed(self, game, player, buy):
        game.engine.set_status(MarketStatus.CLOSED)
        with pytest.raises(MarketClosedError):
            await buy(player["user_id"], "ACME", 1)

    async def test_orders_refused_for_a_halted_symbol(self, game, player, buy):
        game.engine.sims["ACME"].is_halted = True
        with pytest.raises(MarketClosedError):
            await buy(player["user_id"], "ACME", 1)


class TestAntiCheat:
    """A client may ask for an action; it may never assert an outcome."""

    async def test_client_supplied_price_is_ignored(self, game, player, buy):
        result = await game.trading.place_order(
            player["user_id"],
            OrderRequest.parse(
                {
                    "symbol": "ACME",
                    "side": "buy",
                    "type": "market",
                    "quantity": 10,
                    # All of these are attacker-controlled and must be discarded.
                    "price": 1,
                    "price_cents": 1,
                    "cash_cents": 999_999_999,
                    "user_id": 999,
                    "status": "filled",
                    "realized_pl_cents": 500_000,
                }
            ),
        )
        trade = result["trades"][0]
        assert trade["price_cents"] == pytest.approx(game.engine.price_of("ACME"), rel=0.05)
        assert trade["price_cents"] > 100
        assert trade["realized_pl_cents"] == 0

    async def test_order_is_booked_to_the_authenticated_user(self, game, player, rival):
        await game.trading.place_order(
            player["user_id"],
            OrderRequest.parse(
                {
                    "symbol": "ACME",
                    "side": "buy",
                    "type": "market",
                    "quantity": 5,
                    "user_id": rival["user_id"],
                }
            ),
        )
        assert len(await game.trading.list_orders(player["user_id"])) == 1
        assert await game.trading.list_orders(rival["user_id"]) == []

    async def test_players_cannot_see_each_others_orders_or_trades(self, game, player, rival, buy):
        await buy(player["user_id"], "ACME", 5)
        assert await game.trading.list_orders(rival["user_id"]) == []
        assert await game.trading.list_trades(rival["user_id"]) == []

    async def test_public_ids_are_opaque(self, game, player, buy):
        result = await buy(player["user_id"], "ACME", 1)
        order_id = result["order"]["id"]
        # Not a guessable sequential integer.
        assert not order_id.isdigit()
        assert len(order_id) >= 16


class TestMarketImpact:
    async def test_a_large_order_moves_the_price_for_everyone(self, game, player, buy):
        before = game.engine.price_of("ACME")
        await buy(player["user_id"], "ACME", 400)
        assert game.engine.price_of("ACME") > before

    async def test_trades_add_to_reported_volume(self, game, player, buy):
        before = game.engine.sims["ACME"].day_volume
        await buy(player["user_id"], "ACME", 200)
        assert game.engine.sims["ACME"].day_volume >= before + 200
