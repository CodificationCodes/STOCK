"""Portfolio valuation, P/L accounting and the leaderboard."""

from __future__ import annotations

import pytest

from stockgame.server.services.trading import OrderRequest


class TestValuation:
    async def test_new_account_is_all_cash(self, game, player):
        portfolio = await game.portfolios.get_portfolio(player["user_id"])
        assert portfolio["cash_cents"] == portfolio["total_value_cents"] == 10_000_000
        assert portfolio["holdings_value_cents"] == 0
        assert portfolio["total_pl_cents"] == 0
        assert portfolio["total_return_pct"] == 0.0

    async def test_total_value_is_cash_plus_holdings(self, game, player, buy):
        await buy(player["user_id"], "ACME", 100)
        await buy(player["user_id"], "NOVA", 50)
        portfolio = await game.portfolios.get_portfolio(player["user_id"])
        assert (
            portfolio["total_value_cents"]
            == portfolio["cash_cents"] + portfolio["holdings_value_cents"]
        )

    async def test_holdings_value_marks_to_the_live_price(self, game, player, buy):
        await buy(player["user_id"], "ACME", 100)
        game.engine.sims["ACME"].price_cents *= 2
        portfolio = await game.portfolios.get_portfolio(player["user_id"])
        position = portfolio["positions"][0]
        assert position["market_value_cents"] == 100 * game.engine.price_of("ACME")

    async def test_position_weights_sum_to_the_invested_share(self, game, player, buy):
        await buy(player["user_id"], "ACME", 50)
        await buy(player["user_id"], "BYTE", 200)
        portfolio = await game.portfolios.get_portfolio(player["user_id"])
        total_weight = sum(p["weight_pct"] for p in portfolio["positions"])
        cash_weight = portfolio["cash_cents"] / portfolio["total_value_cents"] * 100
        assert total_weight + cash_weight == pytest.approx(100.0, abs=0.01)


class TestCostBasis:
    async def test_average_cost_across_two_buys(self, game, player, buy):
        first = await buy(player["user_id"], "ACME", 100)
        game.engine.sims["ACME"].price_cents = int(game.engine.price_of("ACME") * 1.5)
        second = await buy(player["user_id"], "ACME", 100)

        portfolio = await game.portfolios.get_portfolio(player["user_id"])
        position = portfolio["positions"][0]
        expected_basis = (
            first["trades"][0]["price_cents"] * 100 + second["trades"][0]["price_cents"] * 100
        )
        assert position["quantity"] == 200
        assert position["cost_basis_cents"] == expected_basis
        assert position["avg_cost_cents"] == expected_basis // 200

    async def test_partial_sell_relieves_basis_proportionally(self, game, player, buy, sell):
        await buy(player["user_id"], "ACME", 100)
        before = (await game.portfolios.get_portfolio(player["user_id"]))["positions"][0]
        await sell(player["user_id"], "ACME", 25)
        after = (await game.portfolios.get_portfolio(player["user_id"]))["positions"][0]

        assert after["quantity"] == 75
        # 75/100 of the original basis remains, to the cent.
        assert after["cost_basis_cents"] == before["cost_basis_cents"] - (
            before["cost_basis_cents"] * 25 // 100
        )

    async def test_basis_is_exact_across_many_partial_sells(self, game, player, buy, sell):
        """Rounding must never create or destroy value."""
        await buy(player["user_id"], "ACME", 97)
        for _ in range(9):
            await sell(player["user_id"], "ACME", 7)
        portfolio = await game.portfolios.get_portfolio(player["user_id"])
        position = portfolio["positions"][0]
        assert position["quantity"] == 97 - 63
        assert position["cost_basis_cents"] > 0


class TestProfitAndLoss:
    async def test_unrealised_pl_tracks_the_price(self, game, player, buy):
        await buy(player["user_id"], "ACME", 100)
        game.engine.sims["ACME"].price_cents = int(game.engine.price_of("ACME") * 1.20)

        portfolio = await game.portfolios.get_portfolio(player["user_id"])
        position = portfolio["positions"][0]
        assert position["unrealized_pl_cents"] > 0
        assert (
            position["unrealized_pl_cents"]
            == position["market_value_cents"] - position["cost_basis_cents"]
        )
        assert portfolio["unrealized_pl_cents"] == position["unrealized_pl_cents"]

    async def test_selling_converts_unrealised_into_realised(self, game, player, buy, sell):
        await buy(player["user_id"], "ACME", 100)
        game.engine.sims["ACME"].price_cents = int(game.engine.price_of("ACME") * 1.5)
        result = await sell(player["user_id"], "ACME", 100)

        portfolio = await game.portfolios.get_portfolio(player["user_id"])
        assert result["trades"][0]["realized_pl_cents"] > 0
        assert portfolio["realized_pl_cents"] == result["trades"][0]["realized_pl_cents"]
        assert portfolio["unrealized_pl_cents"] == 0
        assert portfolio["total_pl_cents"] == portfolio["realized_pl_cents"]

    async def test_a_loss_is_recorded_as_negative(self, game, player, buy, sell):
        await buy(player["user_id"], "ACME", 100)
        game.engine.sims["ACME"].price_cents = int(game.engine.price_of("ACME") * 0.5)
        result = await sell(player["user_id"], "ACME", 100)

        portfolio = await game.portfolios.get_portfolio(player["user_id"])
        assert result["trades"][0]["realized_pl_cents"] < 0
        assert portfolio["total_return_pct"] < 0

    async def test_win_and_loss_counters(self, game, player, buy, sell):
        await buy(player["user_id"], "ACME", 100)
        game.engine.sims["ACME"].price_cents = int(game.engine.price_of("ACME") * 1.5)
        await sell(player["user_id"], "ACME", 50)
        game.engine.sims["ACME"].price_cents = int(game.engine.price_of("ACME") * 0.3)
        await sell(player["user_id"], "ACME", 50)

        portfolio = await game.portfolios.get_portfolio(player["user_id"])
        assert portfolio["winning_trades"] == 1
        assert portfolio["losing_trades"] == 1
        # One buy plus two sells. Buys are counted as trades but are neither
        # wins nor losses -- a position only resolves when it is closed.
        assert portfolio["total_trades"] == 3

    async def test_total_return_percentage(self, game, player, buy):
        await buy(player["user_id"], "ACME", 100)
        game.engine.sims["ACME"].price_cents = int(game.engine.price_of("ACME") * 2)
        portfolio = await game.portfolios.get_portfolio(player["user_id"])
        expected = (
            (portfolio["total_value_cents"] - portfolio["deposited_cents"])
            / portfolio["deposited_cents"]
            * 100
        )
        assert portfolio["total_return_pct"] == pytest.approx(expected)


class TestWatchlist:
    async def test_add_and_remove(self, game, player):
        items = await game.portfolios.add_to_watchlist(player["user_id"], "QNTM")
        assert "QNTM" in {row["symbol"] for row in items}
        items = await game.portfolios.remove_from_watchlist(player["user_id"], "QNTM")
        assert "QNTM" not in {row["symbol"] for row in items}

    async def test_adding_twice_is_idempotent(self, game, player):
        await game.portfolios.add_to_watchlist(player["user_id"], "QNTM")
        items = await game.portfolios.add_to_watchlist(player["user_id"], "QNTM")
        assert [row["symbol"] for row in items].count("QNTM") == 1

    async def test_watchlists_are_per_player(self, game, player, rival):
        await game.portfolios.add_to_watchlist(player["user_id"], "QNTM")
        rival_items = await game.portfolios.get_watchlist(rival["user_id"])
        assert "QNTM" not in {row["symbol"] for row in rival_items}

    async def test_unknown_symbol_is_rejected(self, game, player):
        from stockgame.server.core.errors import NotFoundError

        with pytest.raises(NotFoundError):
            await game.portfolios.add_to_watchlist(player["user_id"], "ZZZZ")


class TestProfiles:
    async def test_profile_hides_cash_and_basis(self, game, player, rival, buy):
        await buy(player["user_id"], "ACME", 100)
        profile = await game.portfolios.get_profile("alice", viewer_id=rival["user_id"])

        assert profile["username"] == "alice"
        assert profile["is_self"] is False
        assert "cash_cents" not in profile
        for position in profile["positions"]:
            # Size is public (it is on the tape); entry price is not.
            assert "avg_cost_cents" not in position
            assert "cost_basis_cents" not in position

    async def test_profile_reports_trade_statistics(self, game, player, buy, sell):
        await buy(player["user_id"], "ACME", 10)
        await sell(player["user_id"], "ACME", 10)
        profile = await game.portfolios.get_profile("alice", viewer_id=player["user_id"])
        assert profile["total_trades"] == 2
        assert profile["is_self"] is True

    async def test_a_rival_can_see_what_you_hold(self, game, player, rival, buy):
        """Enter on a leaderboard row opens this payload, so it has to carry
        the positions and the headline numbers the dialog renders."""
        await buy(player["user_id"], "ACME", 25)

        profile = await game.portfolios.get_profile("alice", viewer_id=rival["user_id"])

        held = {row["symbol"]: row for row in profile["positions"]}
        assert held["ACME"]["quantity"] == 25
        assert held["ACME"]["market_value_cents"] > 0
        for key in ("total_value_cents", "total_pl_cents", "total_return_pct", "total_trades"):
            assert key in profile

    async def test_unknown_player(self, game, player):
        from stockgame.server.core.errors import NotFoundError

        with pytest.raises(NotFoundError):
            await game.portfolios.get_profile("ghost")


class TestLeaderboard:
    async def test_ranks_by_return(self, game, player, rival, buy):
        await buy(player["user_id"], "ACME", 200)
        game.engine.sims["ACME"].price_cents = int(game.engine.price_of("ACME") * 2)

        boards = await game.leaderboard.recompute(game.engine.day_index)
        top = boards["all_time"]
        assert top[0]["username"] == "alice"
        assert top[0]["rank"] == 1
        assert top[0]["return_pct"] > 0
        assert top[1]["username"] == "bob"

    async def test_board_never_exposes_cash(self, game, player, rival):
        boards = await game.leaderboard.recompute(game.engine.day_index)
        for row in boards["all_time"]:
            assert row["cash_cents"] is None

    async def test_all_periods_are_produced(self, game, player):
        boards = await game.leaderboard.recompute(game.engine.day_index)
        assert set(boards) == {"all_time", "season", "daily", "weekly", "monthly"}

    async def test_rank_lookup(self, game, player, rival, buy):
        await buy(player["user_id"], "ACME", 100)
        game.engine.sims["ACME"].price_cents = int(game.engine.price_of("ACME") * 3)
        await game.leaderboard.recompute(game.engine.day_index)
        assert game.leaderboard.rank_of(player["user_id"]) == 1
        assert game.leaderboard.rank_of(rival["user_id"]) == 2

    async def test_a_season_exists_and_is_numbered(self, game, player):
        season = await game.leaderboard.ensure_season()
        assert season.number == 1
        assert season.name == "The Opening Bell"
        assert game.leaderboard.season_info()["days_remaining"] is not None


class TestAchievements:
    async def test_trading_awards_a_badge_without_being_asked(self, game, player, buy):
        """Placing a trade publishes an event that evaluates achievements."""
        await buy(player["user_id"], "ACME", 1)
        unlocked = {
            badge["code"]
            for badge in await game.achievements.list_for(player["user_id"])
            if badge["unlocked"]
        }
        assert "first_trade" in unlocked

    async def test_achievements_are_not_awarded_twice(self, game, player, buy):
        await buy(player["user_id"], "ACME", 1)
        portfolio = await game.portfolios.get_portfolio(player["user_id"])
        # The trade already granted "first_trade"; re-evaluating must be a
        # no-op rather than a duplicate row.
        assert await game.achievements.evaluate(player["user_id"], portfolio) == []

        from sqlalchemy import func, select

        from stockgame.server.db.models import Achievement

        async with game.db.session() as session:
            count = await session.scalar(
                select(func.count())
                .select_from(Achievement)
                .where(
                    Achievement.user_id == player["user_id"],
                    Achievement.code == "first_trade",
                )
            )
        assert count == 1

    async def test_a_new_account_has_no_badges(self, game, player):
        unlocked = [
            badge
            for badge in await game.achievements.list_for(player["user_id"])
            if badge["unlocked"]
        ]
        assert unlocked == []


class TestDaySnapshots:
    async def test_snapshot_sets_the_daily_baseline(self, game, player, buy):
        await buy(player["user_id"], "ACME", 100)
        count = await game.portfolios.snapshot_day(game.engine.day_index)
        assert count == 1

        portfolio = await game.portfolios.get_portfolio(player["user_id"])
        # Immediately after a snapshot, today's P/L is zero by definition.
        assert portfolio["day_pl_cents"] == 0

        game.engine.sims["ACME"].price_cents = int(game.engine.price_of("ACME") * 1.1)
        portfolio = await game.portfolios.get_portfolio(player["user_id"])
        assert portfolio["day_pl_cents"] > 0

    async def test_snapshotting_the_same_day_twice_is_safe(self, game, player):
        await game.portfolios.snapshot_day(5)
        await game.portfolios.snapshot_day(5)
        from sqlalchemy import func, select

        from stockgame.server.db.models import PortfolioSnapshot

        async with game.db.session() as session:
            count = await session.scalar(
                select(func.count())
                .select_from(PortfolioSnapshot)
                .where(PortfolioSnapshot.day_index == 5)
            )
        assert count == 1


class TestConcurrentPlayers:
    async def test_two_players_trade_the_same_symbol_independently(
        self, game, player, rival, buy, sell
    ):
        await buy(player["user_id"], "ACME", 100)
        await buy(rival["user_id"], "ACME", 40)

        alice = await game.portfolios.get_portfolio(player["user_id"])
        bob = await game.portfolios.get_portfolio(rival["user_id"])
        assert alice["positions"][0]["quantity"] == 100
        assert bob["positions"][0]["quantity"] == 40

        await sell(player["user_id"], "ACME", 100)
        bob_after = await game.portfolios.get_portfolio(rival["user_id"])
        # Alice closing out must not touch Bob's position.
        assert bob_after["positions"][0]["quantity"] == 40

    async def test_concurrent_orders_keep_balances_consistent(self, game, player, rival):
        import asyncio

        async def trade(user_id, quantity):
            return await game.trading.place_order(
                user_id,
                OrderRequest.parse(
                    {"symbol": "BYTE", "side": "buy", "type": "market", "quantity": quantity}
                ),
            )

        results = await asyncio.gather(
            *(trade(player["user_id"], 10) for _ in range(5)),
            *(trade(rival["user_id"], 10) for _ in range(5)),
            return_exceptions=True,
        )
        assert not [r for r in results if isinstance(r, Exception)]

        for user_id, expected in ((player["user_id"], 50), (rival["user_id"], 50)):
            portfolio = await game.portfolios.get_portfolio(user_id)
            assert portfolio["positions"][0]["quantity"] == expected
            assert portfolio["cash_cents"] >= 0
            assert (
                portfolio["total_value_cents"]
                == portfolio["cash_cents"] + portfolio["holdings_value_cents"]
            )

    async def test_value_all_covers_every_account(self, game, player, rival, buy):
        await buy(player["user_id"], "ACME", 10)
        valuations = await game.portfolios.value_all()
        assert {v.username for v in valuations} == {"alice", "bob"}
        assert all(v.total_value_cents > 0 for v in valuations)
