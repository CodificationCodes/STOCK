"""The market simulation: the price process, the engine and news."""

from __future__ import annotations

import math
import random
import statistics

import pytest

from stockgame.server.market import pricing
from stockgame.server.market.news import NewsGenerator
from stockgame.server.market.pricing import MarketSim, StockSim
from stockgame.server.market.universe import BY_SYMBOL, UNIVERSE
from stockgame.shared.enums import MarketRegime, NewsScope, Sector


def make_sim(**overrides) -> StockSim:
    defaults = {
        "symbol": "TEST",
        "sector": "Technology",
        "price_cents": 10_000,
        "fair_value_cents": 10_000,
        "volatility": 0.38,
        "drift": 0.10,
        "beta": 1.0,
        "liquidity": 0.9,
        "mean_reversion": 0.8,
    }
    defaults.update(overrides)
    return StockSim(**defaults)


class TestUniverse:
    def test_has_enough_companies_across_all_sectors(self):
        assert 30 <= len(UNIVERSE) <= 50
        assert {listing.sector for listing in UNIVERSE} == set(Sector)

    def test_symbols_are_unique_and_well_formed(self):
        symbols = [listing.symbol for listing in UNIVERSE]
        assert len(symbols) == len(set(symbols))
        assert all(symbol.isupper() and 2 <= len(symbol) <= 6 for symbol in symbols)

    def test_companies_have_distinct_personalities(self):
        volatilities = {listing.volatility for listing in UNIVERSE}
        betas = {listing.beta for listing in UNIVERSE}
        assert len(volatilities) > 20
        assert len(betas) > 15
        # At least one hedge-like name, or the whole market moves as one.
        assert any(listing.beta < 0 for listing in UNIVERSE)

    def test_parameters_are_in_sensible_ranges(self):
        for listing in UNIVERSE:
            assert 0.1 < listing.volatility < 1.0
            assert -0.1 < listing.drift < 0.4
            assert 0 < listing.liquidity <= 1.0
            assert listing.price > 1.0
            assert listing.shares_outstanding > 0


class TestPriceProcess:
    def test_prices_stay_positive_over_a_long_run(self):
        rng = random.Random(1)
        market = MarketSim()
        sims = [make_sim(symbol=str(i), volatility=0.85) for i in range(20)]
        for _ in range(5000):
            factor = pricing.market_factor(market, rng, pricing.tick_dt(100))
            for sim in sims:
                pricing.step_stock(sim, market, factor, rng, pricing.tick_dt(100))
        assert all(sim.price_cents >= pricing.MIN_PRICE_CENTS for sim in sims)

    def test_realised_volatility_matches_the_parameter(self):
        """A 38% vol name should move ~2.4% a day, not 0.2% or 20%."""
        rng = random.Random(7)
        market = MarketSim()
        sim = make_sim(volatility=0.38, drift=0.0, beta=0.0, mean_reversion=0.0)
        dt = 1 / pricing.TRADING_DAYS_PER_YEAR
        moves = []
        for _ in range(3000):
            before = sim.price_cents
            pricing.step_stock(sim, market, 0.0, rng, dt)
            moves.append(math.log(sim.price_cents / before))
        realised = statistics.pstdev(moves) * math.sqrt(pricing.TRADING_DAYS_PER_YEAR)
        assert 0.25 < realised < 0.65

    def test_the_model_is_step_size_invariant(self):
        """Stepping 100x per day must not produce 100x the drift.

        This is the bug class that makes a simulated market explode: an
        effect applied per *step* rather than per unit of time.
        """
        results = {}
        for steps_per_day in (1, 50):
            rng = random.Random(11)
            market = MarketSim()
            sim = make_sim(drift=0.15, mean_reversion=0.0)
            dt = pricing.tick_dt(steps_per_day)
            for _ in range(252 * steps_per_day):
                factor = pricing.market_factor(market, rng, dt)
                pricing.step_stock(sim, market, factor, rng, dt)
            results[steps_per_day] = sim.price_cents / 10_000

        # Both should land in the same ballpark after one simulated year.
        assert 0.3 < results[1] < 4.0
        assert 0.3 < results[50] < 4.0

    def test_beta_couples_stocks_to_the_market(self):
        rng = random.Random(3)
        market = MarketSim()
        high = make_sim(beta=2.0, volatility=0.01, drift=0, mean_reversion=0)
        hedge = make_sim(beta=-1.0, volatility=0.01, drift=0, mean_reversion=0)
        dt = 1 / 252
        for _ in range(400):
            factor = pricing.market_factor(market, rng, dt)
            pricing.step_stock(high, market, factor, rng, dt)
            pricing.step_stock(hedge, market, factor, rng, dt)
        # With almost no idiosyncratic noise, a negative beta must move the
        # opposite way to a positive one.
        assert (high.price_cents > 10_000) != (hedge.price_cents > 10_000)

    def test_mean_reversion_pulls_price_back_to_fair_value(self):
        rng = random.Random(5)
        market = MarketSim()
        sim = make_sim(volatility=0.0001, drift=0, mean_reversion=2.0)
        sim.price_cents = 20_000  # 100% above fair value
        for _ in range(400):
            pricing.step_stock(sim, market, 0.0, rng, 1 / 252)
        assert sim.price_cents < 18_000

    def test_volatility_clustering_reacts_to_shocks(self):
        sim = make_sim()
        calm = sim.vol_state
        pricing.apply_shock(sim, 0.20)
        assert sim.vol_state > calm

    def test_volume_scales_with_movement_and_liquidity(self):
        rng = random.Random(2)
        liquid = make_sim(liquidity=1.0)
        thin = make_sim(liquidity=0.1)
        assert pricing.simulated_volume(liquid, 0.0, random.Random(1)) > pricing.simulated_volume(
            thin, 0.0, random.Random(1)
        )
        quiet = statistics.mean(pricing.simulated_volume(liquid, 0.0, rng) for _ in range(200))
        busy = statistics.mean(pricing.simulated_volume(liquid, 0.05, rng) for _ in range(200))
        assert busy > quiet


class TestRegimes:
    def test_every_regime_has_a_profile_and_transitions(self):
        for regime in MarketRegime:
            assert regime in pricing.REGIME_PROFILES
            assert regime in pricing.REGIME_TRANSITIONS
            assert pricing.REGIME_TRANSITIONS[regime]

    def test_bull_drifts_up_and_bear_drifts_down(self):
        assert pricing.REGIME_PROFILES[MarketRegime.BULL].drift > 0
        assert pricing.REGIME_PROFILES[MarketRegime.BEAR].drift < 0
        assert pricing.REGIME_PROFILES[MarketRegime.PANIC].vol_multiplier > 1.5

    def test_regimes_change_over_time(self):
        rng = random.Random(4)
        market = MarketSim()
        seen = set()
        for _ in range(200):
            seen.add(pricing.roll_regime(market, rng, 10))
        assert len(seen) >= 3

    def test_regime_dwell_time_is_positive(self):
        rng = random.Random(6)
        market = MarketSim()
        pricing.roll_regime(market, rng, 100)
        assert market.regime_ticks_left > 0


class TestShocks:
    def test_a_positive_shock_raises_fair_value_and_the_price(self):
        sim = make_sim()
        before_fv = sim.fair_value_cents
        pricing.apply_shock(sim, 0.10)
        assert sim.fair_value_cents > before_fv
        assert sim.event_impact > 0

    def test_a_shock_decays_but_leaves_a_permanent_re_rating(self):
        rng = random.Random(8)
        market = MarketSim()
        sim = make_sim(volatility=0.0001, drift=0, mean_reversion=0)
        pricing.apply_shock(sim, 0.10)
        for _ in range(60):
            pricing.step_stock(sim, market, 0.0, rng, pricing.tick_dt(100))
        assert sim.event_impact == pytest.approx(0.0, abs=1e-4)
        assert sim.price_cents > 10_000  # some of the move stuck

    def test_illiquid_names_gap_harder_on_the_same_news(self):
        liquid = make_sim(liquidity=1.0)
        thin = make_sim(liquidity=0.1)
        pricing.apply_shock(liquid, 0.10)
        pricing.apply_shock(thin, 0.10)
        assert thin.fair_value_cents > liquid.fair_value_cents


class TestNewsGeneration:
    def test_company_news_carries_a_symbol_and_an_impact(self):
        generator = NewsGenerator(random.Random(1))
        item = generator.company_news("ACME", "Acme Technologies", Sector.TECHNOLOGY)
        assert item.scope is NewsScope.COMPANY
        assert item.symbol == "ACME"
        assert item.impact != 0
        assert "{" not in item.headline  # every placeholder was substituted

    def test_sentiment_and_impact_sign_agree(self):
        generator = NewsGenerator(random.Random(2))
        for _ in range(60):
            item = generator.company_news("ACME", "Acme", Sector.TECHNOLOGY)
            if item.sentiment.value == "positive":
                assert item.impact > 0
            elif item.sentiment.value == "negative":
                assert item.impact < 0

    def test_bias_makes_good_news_more_likely(self):
        generator = NewsGenerator(random.Random(3))
        positive = sum(
            generator.company_news("ACME", "Acme", Sector.TECHNOLOGY, bias=0.95).impact > 0
            for _ in range(100)
        )
        assert positive > 70

    def test_a_neutral_bias_does_not_drag_the_market_down(self):
        """Bad headlines hit harder than good ones, so a 50/50 coin flip used
        to bleed the index. At bias 0.5 the *expected impact* must be ~zero."""
        generator = NewsGenerator(random.Random(5))
        for label, draw in (
            ("company", lambda: generator.company_news("ACME", "Acme", Sector.TECHNOLOGY)),
            ("sector", lambda: generator.sector_news(Sector.MINING)),
            ("market", generator.market_news),
        ):
            mean_impact = sum(draw().impact for _ in range(4000)) / 4000
            assert abs(mean_impact) < 0.002, f"{label} news drifts: {mean_impact:+.4f}"

    def test_a_bearish_regime_still_skews_negative(self):
        generator = NewsGenerator(random.Random(6))
        mean_impact = sum(generator.market_news(bias=0.24).impact for _ in range(2000)) / 2000
        assert mean_impact < -0.01

    def test_sector_and_market_news_have_no_symbol(self):
        generator = NewsGenerator(random.Random(4))
        assert generator.sector_news(Sector.ENERGY).symbol is None
        assert generator.market_news().scope is NewsScope.MARKET

    def test_earnings_beats_are_positive(self):
        generator = NewsGenerator(random.Random(5))
        assert generator.earnings("ACME", "Acme", Sector.TECHNOLOGY, beat=True).impact > 0
        assert generator.earnings("ACME", "Acme", Sector.TECHNOLOGY, beat=False).impact < 0


class TestEngine:
    async def test_seeding_creates_the_whole_universe_with_history(self, game):
        assert len(game.engine.sims) == len(UNIVERSE)
        assert game.engine.day_index == 20  # history_days from the test settings

        candles = await game.market_data.candles("ACME", "1M", 100)
        assert len(candles["candles"]) >= 10
        for bar in candles["candles"]:
            assert bar["l"] <= bar["o"] <= bar["h"]
            assert bar["l"] <= bar["c"] <= bar["h"]

    async def test_a_tick_moves_prices_and_reports_them(self, game):
        before = game.engine.prices()
        result = await game.tick_once()
        assert len(result.prices) == len(UNIVERSE)
        assert game.engine.prices() != before

    async def test_seeded_prices_are_near_the_listed_values(self, game):
        """History generation should end up in the right neighbourhood."""
        within = 0
        for symbol, sim in game.engine.sims.items():
            listed = BY_SYMBOL[symbol].price * 100
            if 0.25 < sim.price_cents / listed < 4.0:
                within += 1
        assert within > len(UNIVERSE) * 0.8

    async def test_day_high_and_low_bracket_the_price(self, game):
        for _ in range(30):
            await game.tick_once()
        for symbol, sim in game.engine.sims.items():
            day = game.engine.day_bar(symbol)
            assert day.low_cents <= sim.price_cents <= day.high_cents

    async def test_candles_are_written_on_the_boundary(self, game):
        from sqlalchemy import func, select

        from stockgame.server.db.models import Candle
        from stockgame.server.market.engine import INTRADAY

        for _ in range(game.settings.ticks_per_candle):
            await game.tick_once()
        async with game.db.session() as session:
            count = await session.scalar(
                select(func.count()).select_from(Candle).where(Candle.interval == INTRADAY)
            )
        assert count == len(UNIVERSE)

    async def test_halted_market_does_not_move(self, game):
        from stockgame.shared.enums import MarketStatus

        game.engine.set_status(MarketStatus.HALTED)
        before = game.engine.prices()
        result = await game.tick_once()
        assert result.prices == []
        assert game.engine.prices() == before

    async def test_index_and_sector_summaries_are_produced(self, game):
        await game.tick_once()
        status = game.engine.market_status()
        assert status["index_value"] > 0
        assert status["symbols"] == len(UNIVERSE)

        sectors = game.engine.sector_performance()
        assert len(sectors) == len(Sector)
        assert sectors == sorted(sectors, key=lambda row: row["change_pct"], reverse=True)

    async def test_movers_are_ranked(self, game):
        for _ in range(20):
            await game.tick_once()
        movers = game.engine.movers(3)
        assert len(movers["gainers"]) == 3
        assert movers["gainers"][0]["change_pct"] >= movers["gainers"][-1]["change_pct"]
        assert movers["losers"][0]["change_pct"] <= movers["gainers"][0]["change_pct"]

    async def test_news_is_generated_and_stored_over_time(self, game):
        # Force the dice so the test does not depend on a rare event.
        game.engine.rng = random.Random(1)
        produced = []
        for _ in range(400):
            result = await game.tick_once()
            produced.extend(result.news)
            if produced:
                break
        assert produced
        await game.engine.flush()
        stored = await game.market_data.news(limit=50)
        assert stored


class TestChartData:
    async def test_every_timeframe_returns_ohlc(self, game):
        for timeframe in ("1D", "1W", "1M", "3M", "1Y"):
            payload = await game.market_data.candles("ACME", timeframe, 60)
            assert payload["timeframe"] == timeframe
            assert payload["candles"], timeframe
            assert len(payload["candles"]) <= 60

    async def test_the_last_candle_tracks_the_live_price(self, game):
        payload = await game.market_data.candles("ACME", "1M", 40)
        assert payload["candles"][-1]["c"] == game.engine.price_of("ACME")

    async def test_downsampling_preserves_the_extremes(self):
        from stockgame.server.services.market_data import _downsample

        rows = [{"t": str(i), "o": i, "h": i + 10, "l": i - 10, "c": i, "v": 1} for i in range(100)]
        reduced = _downsample(rows, 10)
        assert len(reduced) == 10
        assert reduced[0]["o"] == rows[0]["o"]
        assert reduced[-1]["c"] == rows[-1]["c"]
        assert max(bar["h"] for bar in reduced) == max(bar["h"] for bar in rows)
        assert min(bar["l"] for bar in reduced) == min(bar["l"] for bar in rows)
        assert sum(bar["v"] for bar in reduced) == sum(bar["v"] for bar in rows)

    async def test_unknown_symbol_raises(self, game):
        with pytest.raises(KeyError):
            await game.market_data.candles("ZZZZ", "1D", 10)
