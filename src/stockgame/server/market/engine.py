"""The market engine: owns the world clock, prices, candles and news.

Design notes
------------
*In-memory authority, periodic durability.* Only this process mutates prices,
so the live state lives in memory and is flushed to the database on candle
boundaries and day rollovers. That keeps a 2-second tick across 47 symbols
cheap while still surviving a restart with at most one candle of loss.

*Two candle resolutions.* Intraday bars (``interval='i'``) back the 1D chart
and are pruned after a couple of simulated days; end-of-day bars
(``interval='d'``) back 1W/1M/3M/1Y and are kept forever.

*History is generated, not accumulated.* On first run the engine fast-forwards
the same price model at daily resolution to produce a year-plus of end-of-day
candles, so charts are useful from the very first login.
"""

from __future__ import annotations

import logging
import math
import random
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select, update

from stockgame.server.config import Settings
from stockgame.server.db.models import Candle, MarketEvent, MarketState, Stock
from stockgame.server.db.session import Database
from stockgame.server.market import pricing
from stockgame.server.market.hours import TradingHours
from stockgame.server.market.news import GeneratedNews, NewsGenerator
from stockgame.server.market.pricing import MarketSim, StockSim
from stockgame.server.market.universe import UNIVERSE
from stockgame.shared.enums import MarketRegime, MarketStatus, Sector

log = logging.getLogger(__name__)

INTRADAY = "i"
DAILY = "d"

#: Chance per tick of a company headline somewhere in the market.
COMPANY_NEWS_CHANCE = 0.010
SECTOR_NEWS_CHANCE = 0.0015
MARKET_NEWS_CHANCE = 0.0006
#: How long a sector rotation's drift tilt persists, in simulated days.
SECTOR_TILT_DAYS = 4
#: Simulated days between a company's earnings reports.
EARNINGS_PERIOD_DAYS = 63
#: Simulated days of intraday candles to retain.
INTRADAY_RETENTION_DAYS = 2


@dataclass(slots=True)
class LiveCandle:
    """The intraday bar currently being accumulated for one stock."""

    open_cents: int
    high_cents: int
    low_cents: int
    close_cents: int
    volume_start: int

    def update(self, price_cents: int) -> None:
        self.close_cents = price_cents
        if price_cents > self.high_cents:
            self.high_cents = price_cents
        if price_cents < self.low_cents:
            self.low_cents = price_cents


@dataclass(slots=True)
class DayBar:
    """Per-stock accumulator for the current simulated day."""

    open_cents: int
    high_cents: int
    low_cents: int
    prev_close_cents: int


class MarketEngine:
    """Advances the simulated market and persists it."""

    def __init__(self, db: Database, settings: Settings) -> None:
        self.db = db
        self.settings = settings
        self.rng = random.Random()
        self.news_rng = random.Random()
        self.news = NewsGenerator(self.news_rng)

        self.sims: dict[str, StockSim] = {}
        self.stock_ids: dict[str, int] = {}
        self.symbols_by_id: dict[int, str] = {}
        self.names: dict[str, str] = {}
        self.market = MarketSim()
        self.status = MarketStatus.OPEN
        self.hours = TradingHours.from_settings(settings)

        self.day_index = 0
        self.tick_count = 0
        self.candle_ticks = 0
        self.day_started_at = datetime.now(timezone.utc)
        self.index_value = 100_000
        self.index_open = 100_000
        #: Base index divisor, fixed at seed time so the index is comparable.
        self._index_divisor = 1.0

        self._live: dict[str, LiveCandle] = {}
        self._day: dict[str, DayBar] = {}
        self._sector_tilt_expiry: dict[str, int] = {}
        self._pending_news: list[GeneratedNews] = []
        self._loaded = False

    # -- lifecycle ----------------------------------------------------------

    @property
    def ticks_per_day(self) -> int:
        return max(1, int(self.settings.day_seconds / self.settings.tick_seconds))

    @property
    def dt(self) -> float:
        return pricing.tick_dt(self.ticks_per_day)

    async def load(self) -> None:
        """Seed the world on first run, then hydrate memory from the database."""
        async with self.db.write_session() as session:
            state = await session.get(MarketState, 1)
            if state is None:
                state = MarketState(id=1)
                session.add(state)
                await session.flush()
            if not state.seeded:
                log.info("No market found -- seeding a new world (this happens once)")
                await self._seed(session, state)
                state.seeded = True
            await session.flush()
            await self._hydrate(session, state)
        self._loaded = True
        log.info(
            "Market ready: %d symbols, day %d, regime=%s",
            len(self.sims),
            self.day_index,
            self.market.regime,
        )

    async def _hydrate(self, session, state: MarketState) -> None:
        rows = (await session.execute(select(Stock))).scalars().all()
        self.sims.clear()
        self.stock_ids.clear()
        self.symbols_by_id.clear()
        for row in rows:
            self.stock_ids[row.symbol] = row.id
            self.symbols_by_id[row.id] = row.symbol
            self.names[row.symbol] = row.name
            sim = StockSim(
                symbol=row.symbol,
                sector=row.sector,
                price_cents=row.price_cents,
                fair_value_cents=row.fair_value_cents,
                volatility=row.volatility,
                drift=row.drift,
                beta=row.beta,
                liquidity=row.liquidity,
                mean_reversion=row.mean_reversion,
                momentum=row.momentum,
                event_impact=row.event_impact,
                day_volume=row.day_volume,
                is_halted=row.is_halted,
            )
            self.sims[row.symbol] = sim
            self._day[row.symbol] = DayBar(
                open_cents=row.open_cents,
                high_cents=row.day_high_cents,
                low_cents=row.day_low_cents,
                prev_close_cents=row.prev_close_cents,
            )
            self._live[row.symbol] = LiveCandle(
                open_cents=row.price_cents,
                high_cents=row.price_cents,
                low_cents=row.price_cents,
                close_cents=row.price_cents,
                volume_start=row.day_volume,
            )

        self.market.regime = MarketRegime(state.regime)
        self.market.regime_ticks_left = state.regime_ticks_left
        self.status = MarketStatus(state.status)
        self.day_index = state.day_index
        self.tick_count = state.tick_count
        self.candle_ticks = state.candle_ticks
        self.day_started_at = state.day_started_at
        self.index_open = state.index_open_cents
        self._recompute_divisor()
        self.index_value = self._compute_index()

        self._sync_schedule()
        log.info("Trading hours: %s", self.hours.describe())
        # A server that was down over the break resumes with a stale clock;
        # without this the first tick would immediately roll a day.
        if self._day_elapsed() >= self.settings.day_seconds:
            self.day_started_at = datetime.now(timezone.utc)

    def _day_elapsed(self) -> float:
        return (datetime.now(timezone.utc) - self.day_started_at).total_seconds()

    def _sync_schedule(self) -> MarketStatus | None:
        """Align status with the wall clock. Returns the new status if it moved.

        A halt is an operator decision, so the schedule never overrides it.
        """
        if self.status is MarketStatus.HALTED:
            return None

        now = datetime.now(timezone.utc)
        should_open = self.settings.market_open and self.hours.is_open(now)
        desired = MarketStatus.OPEN if should_open else MarketStatus.CLOSED
        if desired is self.status:
            return None

        self.status = desired
        if desired is MarketStatus.OPEN:
            # Don't count the overnight break against the current day.
            self.day_started_at = now
            log.info("Market open (day %d)", self.day_index)
        else:
            log.info("Market closed until %s", self.hours.next_open(now).isoformat())
        return desired

    # -- seeding ------------------------------------------------------------

    async def _seed(self, session, state: MarketState) -> None:
        """Create the universe and fast-forward a year of daily candles."""
        seed_rng = random.Random(self.settings.market_seed)
        news_rng = random.Random(self.settings.market_seed + 1)
        generator = NewsGenerator(news_rng)

        sims: dict[str, StockSim] = {}
        rows: dict[str, Stock] = {}
        for listing in UNIVERSE:
            price_cents = round(listing.price * 100)
            # Start prices back in time so the generated history ends near the
            # listed price rather than drifting far away from it.
            start_cents = max(
                pricing.MIN_PRICE_CENTS,
                int(price_cents * math.exp(-listing.drift * self.settings.history_days / 252)),
            )
            sim = StockSim(
                symbol=listing.symbol,
                sector=str(listing.sector.value),
                price_cents=start_cents,
                fair_value_cents=start_cents,
                volatility=listing.volatility,
                drift=listing.drift,
                beta=listing.beta,
                liquidity=listing.liquidity,
                mean_reversion=0.6 + 0.8 * (1 - listing.liquidity),
            )
            sims[listing.symbol] = sim
            row = Stock(
                symbol=listing.symbol,
                name=listing.name,
                sector=str(listing.sector.value),
                description=listing.description,
                price_cents=start_cents,
                prev_close_cents=start_cents,
                open_cents=start_cents,
                day_high_cents=start_cents,
                day_low_cents=start_cents,
                day_volume=0,
                shares_outstanding=listing.shares_outstanding,
                volatility=listing.volatility,
                drift=listing.drift,
                beta=listing.beta,
                liquidity=listing.liquidity,
                momentum=0.0,
                mean_reversion=sim.mean_reversion,
                fair_value_cents=start_cents,
                event_impact=0.0,
            )
            rows[listing.symbol] = row
            session.add(row)
        await session.flush()

        market = MarketSim(regime=MarketRegime.NEUTRAL)
        pricing.roll_regime(market, seed_rng, ticks_per_day=1)
        history_start = datetime.now(timezone.utc) - timedelta(days=self.settings.history_days)
        daily_dt = 1.0 / pricing.TRADING_DAYS_PER_YEAR
        candles: list[Candle] = []
        events: list[MarketEvent] = []

        for day in range(self.settings.history_days):
            ts = history_start + timedelta(days=day)
            if market.regime_ticks_left <= 0:
                pricing.roll_regime(market, seed_rng, ticks_per_day=1)
            market.regime_ticks_left -= 1
            bias = market.profile.news_bias

            # Occasional headlines so the historical charts have real events
            # in them. Only recent ones are persisted for the news feed.
            recent = day >= self.settings.history_days - 45
            if seed_rng.random() < 0.55:
                listing = seed_rng.choice(UNIVERSE)
                item = generator.company_news(
                    listing.symbol, listing.name, listing.sector, bias=bias
                )
                pricing.apply_shock(sims[listing.symbol], item.impact)
                if recent:
                    events.append(self._event_row(item, rows[listing.symbol].id, day, ts))
            if seed_rng.random() < 0.10:
                sector = seed_rng.choice(tuple(Sector))
                item = generator.sector_news(sector, bias=bias)
                for sim in sims.values():
                    if sim.sector == str(sector.value):
                        pricing.apply_shock(sim, item.impact, fair_value_share=0.5)
                if recent:
                    events.append(self._event_row(item, None, day, ts))
            if seed_rng.random() < 0.05:
                item = generator.market_news(bias=bias)
                pricing.apply_market_shock(market, item.impact)
                if recent:
                    events.append(self._event_row(item, None, day, ts))

            factor = pricing.market_factor(market, seed_rng, daily_dt)
            for symbol, sim in sims.items():
                open_cents = sim.price_cents
                sim.day_volume = 0
                log_return = pricing.step_stock(sim, market, factor, seed_rng, daily_dt)
                close_cents = sim.price_cents
                high, low = self._synthesise_wick(
                    open_cents, close_cents, sim, log_return, seed_rng
                )
                candles.append(
                    Candle(
                        stock_id=rows[symbol].id,
                        interval=DAILY,
                        day_index=day,
                        ts=ts,
                        open_cents=open_cents,
                        high_cents=high,
                        low_cents=low,
                        close_cents=close_cents,
                        # Daily volume is ~ a full session of tick volume.
                        volume=sim.day_volume * self.ticks_per_day // 4 + 1,
                    )
                )

        session.add_all(candles)
        session.add_all(events)

        # Commit the simulated present back onto the stock rows.
        for symbol, sim in sims.items():
            row = rows[symbol]
            row.price_cents = sim.price_cents
            row.prev_close_cents = sim.price_cents
            row.open_cents = sim.price_cents
            row.day_high_cents = sim.price_cents
            row.day_low_cents = sim.price_cents
            row.day_volume = 0
            row.momentum = sim.momentum
            row.fair_value_cents = sim.fair_value_cents
            row.event_impact = 0.0

        state.day_index = self.settings.history_days
        state.regime = str(market.regime.value)
        state.regime_ticks_left = 0
        state.tick_count = 0
        state.candle_ticks = 0
        state.day_started_at = datetime.now(timezone.utc)
        state.status = str(MarketStatus.OPEN.value)
        log.info(
            "Seeded %d symbols with %d days of history (%d candles, %d headlines)",
            len(sims),
            self.settings.history_days,
            len(candles),
            len(events),
        )

    def _synthesise_wick(
        self,
        open_cents: int,
        close_cents: int,
        sim: StockSim,
        log_return: float,
        rng: random.Random,
    ) -> tuple[int, int]:
        """Plausible daily high/low around a known open and close."""
        body_high = max(open_cents, close_cents)
        body_low = min(open_cents, close_cents)
        daily_sigma = sim.volatility / math.sqrt(pricing.TRADING_DAYS_PER_YEAR)
        wick = daily_sigma * (0.3 + rng.random() * 0.9)
        high = int(body_high * (1 + wick * rng.random()))
        low = int(body_low * (1 - wick * rng.random()))
        return max(high, body_high), max(pricing.MIN_PRICE_CENTS, min(low, body_low))

    def _event_row(
        self, item: GeneratedNews, stock_id: int | None, day: int, ts: datetime
    ) -> MarketEvent:
        return MarketEvent(
            scope=str(item.scope.value),
            sentiment=str(item.sentiment.value),
            headline=item.headline,
            body=item.body,
            stock_id=stock_id,
            symbol=item.symbol,
            sector=item.sector,
            impact=item.impact,
            day_index=day,
            created_at=ts,
        )

    # -- the tick -----------------------------------------------------------

    async def tick(self) -> TickResult:
        """Advance the market one tick. Returns everything worth broadcasting."""
        if not self._loaded:
            raise RuntimeError("MarketEngine.load() must be called before tick()")

        result = TickResult(day_index=self.day_index, tick=self.tick_count)
        result.status_changed = self._sync_schedule()
        if self.status is not MarketStatus.OPEN:
            result.status = self.status
            return result

        self.tick_count += 1
        self.candle_ticks += 1

        if self.market.regime_ticks_left <= 0:
            previous = self.market.regime
            pricing.roll_regime(self.market, self.rng, self.ticks_per_day)
            if self.market.regime is not previous:
                result.regime_changed = self.market.regime
                log.info("Market regime: %s -> %s", previous, self.market.regime)
        self.market.regime_ticks_left -= 1

        result.news.extend(self._maybe_generate_news())

        factor = pricing.market_factor(self.market, self.rng, self.dt)
        for symbol, sim in self.sims.items():
            pricing.step_stock(sim, self.market, factor, self.rng, self.dt)
            live = self._live[symbol]
            live.update(sim.price_cents)
            day = self._day[symbol]
            if sim.price_cents > day.high_cents:
                day.high_cents = sim.price_cents
            if sim.price_cents < day.low_cents:
                day.low_cents = sim.price_cents

        self.index_value = self._compute_index()
        result.prices = self.price_ticks()

        if self.candle_ticks >= self.settings.ticks_per_candle:
            await self._close_candle()
            result.candle_closed = True

        if self._day_elapsed() >= self.settings.day_seconds:
            await self._roll_day()
            result.day_rolled = self.day_index

        result.regime = self.market.regime
        result.index_value = self.index_value
        result.index_change_pct = (
            (self.index_value - self.index_open) / self.index_open * 100 if self.index_open else 0.0
        )
        return result

    def _maybe_generate_news(self) -> list[GeneratedNews]:
        """Roll for headlines and apply their shocks immediately."""
        produced: list[GeneratedNews] = []
        bias = self.market.profile.news_bias

        if self.rng.random() < COMPANY_NEWS_CHANCE:
            symbol = self.rng.choice(list(self.sims))
            sim = self.sims[symbol]
            item = self.news.company_news(
                symbol, self.names.get(symbol, symbol), Sector(sim.sector), bias=bias
            )
            pricing.apply_shock(sim, item.impact)
            produced.append(item)

        if self.rng.random() < SECTOR_NEWS_CHANCE:
            sector = self.rng.choice(tuple(Sector))
            item = self.news.sector_news(sector, bias=bias)
            # A sector event is both an immediate shock and a multi-day tilt.
            self.market.sector_tilts[str(sector.value)] = item.impact * 3.0
            self._sector_tilt_expiry[str(sector.value)] = self.day_index + SECTOR_TILT_DAYS
            for sim in self.sims.values():
                if sim.sector == str(sector.value):
                    pricing.apply_shock(sim, item.impact * 0.6, fair_value_share=0.5)
            produced.append(item)

        if self.rng.random() < MARKET_NEWS_CHANCE:
            item = self.news.market_news(bias=bias)
            pricing.apply_market_shock(self.market, item.impact)
            # Big macro shocks can tip the whole market into a new regime.
            if abs(item.impact) > 0.045:
                self.market.regime_ticks_left = 0
            produced.append(item)

        self._pending_news.extend(produced)
        return produced

    async def _close_candle(self) -> None:
        """Persist one intraday bar per symbol and flush live stock state."""
        self.candle_ticks = 0
        now = datetime.now(timezone.utc)
        candles = []
        for symbol, sim in self.sims.items():
            live = self._live[symbol]
            candles.append(
                Candle(
                    stock_id=self.stock_ids[symbol],
                    interval=INTRADAY,
                    day_index=self.day_index,
                    ts=now,
                    open_cents=live.open_cents,
                    high_cents=live.high_cents,
                    low_cents=live.low_cents,
                    close_cents=live.close_cents,
                    volume=max(0, sim.day_volume - live.volume_start),
                )
            )
            self._live[symbol] = LiveCandle(
                open_cents=sim.price_cents,
                high_cents=sim.price_cents,
                low_cents=sim.price_cents,
                close_cents=sim.price_cents,
                volume_start=sim.day_volume,
            )

        async with self.db.write_session() as session:
            session.add_all(candles)
            await self._flush_stocks(session)
            await self._flush_news(session)
            await self._flush_state(session)

    async def _flush_stocks(self, session) -> None:
        payload = []
        for symbol, sim in self.sims.items():
            day = self._day[symbol]
            payload.append(
                {
                    "id": self.stock_ids[symbol],
                    "price_cents": sim.price_cents,
                    "day_high_cents": day.high_cents,
                    "day_low_cents": day.low_cents,
                    "day_volume": sim.day_volume,
                    "momentum": sim.momentum,
                    "fair_value_cents": sim.fair_value_cents,
                    "event_impact": sim.event_impact,
                }
            )
        # ORM bulk update by primary key: one executemany rather than 47
        # round trips. synchronize_session=None because the engine, not the
        # identity map, is the source of truth for prices.
        await session.execute(
            update(Stock), payload, execution_options={"synchronize_session": None}
        )

    async def _flush_news(self, session) -> None:
        if not self._pending_news:
            return
        now = datetime.now(timezone.utc)
        for item in self._pending_news:
            stock_id = self.stock_ids.get(item.symbol) if item.symbol else None
            session.add(self._event_row(item, stock_id, self.day_index, now))
        self._pending_news.clear()

    async def _flush_state(self, session) -> None:
        await session.execute(
            update(MarketState)
            .where(MarketState.id == 1)
            .values(
                status=str(self.status.value),
                regime=str(self.market.regime.value),
                regime_ticks_left=self.market.regime_ticks_left,
                day_index=self.day_index,
                day_started_at=self.day_started_at,
                tick_count=self.tick_count,
                candle_ticks=self.candle_ticks,
                index_value=self.index_value,
                index_open_cents=self.index_open,
            )
        )

    async def _roll_day(self) -> None:
        """Close the simulated day: write daily bars, reset session stats."""
        now = datetime.now(timezone.utc)
        closing_day = self.day_index
        candles = []
        for symbol, sim in self.sims.items():
            day = self._day[symbol]
            candles.append(
                Candle(
                    stock_id=self.stock_ids[symbol],
                    interval=DAILY,
                    day_index=closing_day,
                    ts=now,
                    open_cents=day.open_cents,
                    high_cents=day.high_cents,
                    low_cents=day.low_cents,
                    close_cents=sim.price_cents,
                    volume=sim.day_volume,
                )
            )

        self.day_index += 1
        self.day_started_at = now
        self.index_open = self.index_value

        # Expire finished sector rotations.
        for sector, expiry in list(self._sector_tilt_expiry.items()):
            if self.day_index >= expiry:
                self.market.sector_tilts.pop(sector, None)
                self._sector_tilt_expiry.pop(sector, None)

        # Overnight gap + fresh session stats. Today's close becomes tomorrow's
        # reference price, so the change column measures close-to-last, gap
        # included -- which is what a real quote screen shows.
        for symbol, sim in self.sims.items():
            closing_price = sim.price_cents
            gap = self.rng.gauss(0.0, sim.volatility / math.sqrt(252) * 0.35)
            sim.price_cents = max(pricing.MIN_PRICE_CENTS, round(closing_price * math.exp(gap)))
            sim.day_volume = 0
            self._day[symbol] = DayBar(
                open_cents=sim.price_cents,
                high_cents=sim.price_cents,
                low_cents=sim.price_cents,
                prev_close_cents=closing_price,
            )
            self._live[symbol] = LiveCandle(
                open_cents=sim.price_cents,
                high_cents=sim.price_cents,
                low_cents=sim.price_cents,
                close_cents=sim.price_cents,
                volume_start=0,
            )

        self._schedule_earnings()

        async with self.db.write_session() as session:
            session.add_all(candles)
            await session.execute(
                update(Stock),
                [
                    {
                        "id": self.stock_ids[symbol],
                        "price_cents": sim.price_cents,
                        "prev_close_cents": self._day[symbol].prev_close_cents,
                        "open_cents": sim.price_cents,
                        "day_high_cents": sim.price_cents,
                        "day_low_cents": sim.price_cents,
                        "day_volume": 0,
                        "momentum": sim.momentum,
                        "fair_value_cents": sim.fair_value_cents,
                        "event_impact": sim.event_impact,
                    }
                    for symbol, sim in self.sims.items()
                ],
                execution_options={"synchronize_session": None},
            )
            await session.execute(
                delete(Candle).where(
                    Candle.interval == INTRADAY,
                    Candle.day_index < self.day_index - INTRADAY_RETENTION_DAYS,
                )
            )
            await self._flush_news(session)
            await self._flush_state(session)
        log.info("Simulated day %d closed; now on day %d", closing_day, self.day_index)

    def _schedule_earnings(self) -> None:
        """Report earnings for the companies whose quarter ends today."""
        for index, (symbol, sim) in enumerate(self.sims.items()):
            if (self.day_index + index) % EARNINGS_PERIOD_DAYS:
                continue
            # A quality company beats more often than it misses.
            beat_chance = 0.5 + max(-0.25, min(0.25, sim.drift))
            beat = self.rng.random() < beat_chance
            item = self.news.earnings(
                symbol, self.names.get(symbol, symbol), Sector(sim.sector), beat=beat
            )
            pricing.apply_shock(sim, item.impact, fair_value_share=0.75)
            self._pending_news.append(item)

    # -- queries ------------------------------------------------------------

    def _recompute_divisor(self) -> None:
        total = sum(sim.price_cents for sim in self.sims.values())
        self._index_divisor = (total / 100_000) if total else 1.0

    def _compute_index(self) -> int:
        if not self.sims or not self._index_divisor:
            return self.index_value
        total = sum(sim.price_cents for sim in self.sims.values())
        return int(total / self._index_divisor)

    def day_bar(self, symbol: str) -> DayBar:
        """Today's open/high/low accumulator for a symbol."""
        return self._day[symbol]

    def price_of(self, symbol: str) -> int | None:
        sim = self.sims.get(symbol)
        return sim.price_cents if sim else None

    def prices(self) -> dict[str, int]:
        return {symbol: sim.price_cents for symbol, sim in self.sims.items()}

    def price_ticks(self) -> list[dict[str, object]]:
        """Compact per-symbol payload for the market channel."""
        ticks = []
        for symbol, sim in self.sims.items():
            day = self._day[symbol]
            prev = day.prev_close_cents or sim.price_cents
            ticks.append(
                {
                    "s": symbol,
                    "p": sim.price_cents,
                    "c": round((sim.price_cents - prev) / prev * 100, 3) if prev else 0.0,
                    "v": sim.day_volume,
                    "h": day.high_cents,
                    "l": day.low_cents,
                }
            )
        return ticks

    def snapshot(self) -> list[dict[str, object]]:
        """Full market listing, used for the initial market subscribe."""
        rows = []
        for symbol, sim in self.sims.items():
            day = self._day[symbol]
            prev = day.prev_close_cents or sim.price_cents
            rows.append(
                {
                    "symbol": symbol,
                    "name": self.names.get(symbol, symbol),
                    "sector": sim.sector,
                    "price_cents": sim.price_cents,
                    "prev_close_cents": prev,
                    "open_cents": day.open_cents,
                    "day_high_cents": day.high_cents,
                    "day_low_cents": day.low_cents,
                    "volume": sim.day_volume,
                    "change_pct": round((sim.price_cents - prev) / prev * 100, 3) if prev else 0.0,
                    "volatility": sim.volatility,
                    "is_halted": sim.is_halted,
                }
            )
        rows.sort(key=lambda row: row["symbol"])
        return rows

    def market_status(self) -> dict[str, object]:
        return {
            "status": str(self.status.value),
            "regime": str(self.market.regime.value),
            "day_index": self.day_index,
            "tick": self.tick_count,
            "ticks_per_day": self.ticks_per_day,
            "day_seconds": self.settings.day_seconds,
            "tick_seconds": self.settings.tick_seconds,
            "day_elapsed": self._day_elapsed(),
            "hours": self.hours.describe(),
            "next_open": (
                None
                if self.status is MarketStatus.OPEN
                else self.hours.next_open(datetime.now(timezone.utc)).isoformat()
            ),
            "index_value": self.index_value,
            "index_change_pct": (
                (self.index_value - self.index_open) / self.index_open * 100
                if self.index_open
                else 0.0
            ),
            "symbols": len(self.sims),
        }

    def set_status(self, status: MarketStatus) -> None:
        self.status = status
        log.warning("Market status set to %s", status)

    def sector_performance(self) -> list[dict[str, object]]:
        """Average intraday move per sector, for the market overview panel."""
        buckets: dict[str, list[float]] = {}
        for symbol, sim in self.sims.items():
            prev = self._day[symbol].prev_close_cents or sim.price_cents
            change = (sim.price_cents - prev) / prev * 100 if prev else 0.0
            buckets.setdefault(sim.sector, []).append(change)
        return sorted(
            (
                {
                    "sector": sector,
                    "change_pct": round(sum(values) / len(values), 3),
                    "count": len(values),
                }
                for sector, values in buckets.items()
            ),
            key=lambda row: row["change_pct"],
            reverse=True,
        )

    def movers(self, limit: int = 5) -> dict[str, list[dict[str, object]]]:
        ranked = sorted(self.snapshot(), key=lambda row: row["change_pct"], reverse=True)
        return {"gainers": ranked[:limit], "losers": list(reversed(ranked[-limit:]))}

    async def flush(self) -> None:
        """Persist everything now (used on shutdown and by the admin CLI)."""
        async with self.db.write_session() as session:
            await self._flush_stocks(session)
            await self._flush_news(session)
            await self._flush_state(session)


@dataclass(slots=True)
class TickResult:
    """What changed in one tick, for the broadcaster to fan out."""

    day_index: int
    tick: int
    prices: list[dict[str, object]] = None  # type: ignore[assignment]
    news: list[GeneratedNews] = None  # type: ignore[assignment]
    regime: MarketRegime | None = None
    regime_changed: MarketRegime | None = None
    day_rolled: int | None = None
    candle_closed: bool = False
    status: MarketStatus | None = None
    #: Set only on the tick where the wall-clock schedule opened or closed us.
    status_changed: MarketStatus | None = None
    index_value: int = 0
    index_change_pct: float = 0.0

    def __post_init__(self) -> None:
        if self.prices is None:
            self.prices = []
        if self.news is None:
            self.news = []
