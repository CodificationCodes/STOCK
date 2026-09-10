"""The game server: composition root and the loop that drives the world.

Holds one instance of each service and runs three background tasks:

``_tick_loop``         advances the market, fills resting orders, broadcasts.
``_leaderboard_loop``  recomputes rankings on a slower cadence.
``_maintenance_loop``  prunes expired sessions and rate-limit buckets.

The loop is deliberately drift-corrected: it sleeps until the *next scheduled*
tick rather than for a fixed interval, so a slow tick does not permanently
push the simulated clock behind wall-clock time.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from typing import Any

from stockgame.server.config import Settings
from stockgame.server.core.events import Event, EventBus, Topics
from stockgame.server.core.rate_limit import RateLimiter
from stockgame.server.db.session import Database
from stockgame.server.market.engine import MarketEngine
from stockgame.server.services.achievements import AchievementService
from stockgame.server.services.auth import AuthService
from stockgame.server.services.insider import InsiderService
from stockgame.server.services.leaderboard import LeaderboardService
from stockgame.server.services.market_data import MarketDataService
from stockgame.server.services.portfolio import PortfolioService
from stockgame.server.services.trading import TradingService
from stockgame.shared.enums import MarketStatus

log = logging.getLogger("stockgame.game")

MAINTENANCE_INTERVAL = 600


class GameServer:
    """Owns every long-lived object in the process."""

    def __init__(self, settings: Settings, db: Database | None = None) -> None:
        self.settings = settings
        self.db = db or Database(settings)
        self.bus = EventBus()

        self.engine = MarketEngine(self.db, settings)
        self.auth = AuthService(self.db, settings)
        self.portfolios = PortfolioService(self.db, settings, self.engine)
        self.trading = TradingService(self.db, settings, self.engine, self.bus)
        self.leaderboard = LeaderboardService(self.db, settings, self.portfolios, self.bus)
        self.achievements = AchievementService(self.db, self.bus)
        self.insider = InsiderService(self.db, settings, self.engine)
        self.market_data = MarketDataService(self.db, self.engine)

        self.auth_limiter = RateLimiter(settings.auth_rate_limit, settings.auth_rate_window_seconds)
        self.order_limiter = RateLimiter(
            settings.order_rate_limit, settings.order_rate_window_seconds
        )

        #: Called with each :class:`TickResult`. Price fan-out goes here rather
        #: than through the event bus: it fires 30x a minute and the payload is
        #: routed by subscription, not by topic.
        self.tick_listeners: list[Callable[[Any], Awaitable[None]]] = []

        self._tasks: list[asyncio.Task] = []
        self._running = False
        self.started_at = time.time()
        self.ticks_processed = 0

        # Awarding badges is a side effect of trading, wired here so the
        # trading service stays unaware that achievements exist.
        self.bus.subscribe(Topics.PORTFOLIO_CHANGED, self._on_portfolio_changed)

    # -- lifecycle ----------------------------------------------------------

    async def start(self, *, run_loops: bool = True) -> None:
        await self.db.create_all()
        await self.engine.load()
        await self.leaderboard.ensure_season()
        await self.leaderboard.recompute(self.engine.day_index)
        if run_loops:
            self._running = True
            self._tasks = [
                asyncio.create_task(self._tick_loop(), name="market-tick"),
                asyncio.create_task(self._leaderboard_loop(), name="leaderboard"),
                asyncio.create_task(self._maintenance_loop(), name="maintenance"),
            ]
        log.info(
            "Game server ready (tick=%.1fs, simulated day=%ds, %d symbols)",
            self.settings.tick_seconds,
            self.settings.day_seconds,
            len(self.engine.sims),
        )

    async def stop(self) -> None:
        self._running = False
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            # Shutdown is best effort: a loop that died on its way out must
            # not stop the rest of the server from closing cleanly.
            with contextlib.suppress(BaseException):
                await task
        self._tasks.clear()
        try:
            await self.engine.flush()
        except Exception:
            log.exception("failed to flush market state on shutdown")
        await self.db.dispose()
        log.info("Game server stopped after %d ticks", self.ticks_processed)

    # -- loops --------------------------------------------------------------

    async def _tick_loop(self) -> None:
        interval = self.settings.tick_seconds
        next_at = time.monotonic()
        while self._running:
            next_at += interval
            try:
                await self.tick_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                # One bad tick must never kill the world.
                log.exception("market tick failed")
            delay = next_at - time.monotonic()
            if delay < 0:
                # Fell behind: resynchronise rather than sprint to catch up.
                log.warning("tick loop is %.2fs behind; resyncing", -delay)
                next_at = time.monotonic()
                delay = 0
            await asyncio.sleep(delay)

    async def tick_once(self) -> Any:
        """One iteration of the world. Exposed so tests can step deterministically."""
        result = await self.engine.tick()
        self.ticks_processed += 1

        if result.prices:
            # Tips land before orders settle, so a bought rumour is already in
            # the price when the orders chasing it fill.
            await self.insider.apply_due_tips()
            await self.trading.process_resting_orders()

        if result.day_rolled is not None:
            await self.portfolios.snapshot_day(result.day_rolled)
            await self.leaderboard.ensure_season()
            await self.leaderboard.recompute(self.engine.day_index)

        if result.regime_changed or result.day_rolled is not None or result.status_changed:
            await self.bus.publish(Event(Topics.MARKET_STATUS, self.engine.market_status()))
        published_at = datetime.now(timezone.utc).isoformat()
        for item in result.news:
            await self.bus.publish(
                Event(
                    Topics.NEWS_PUBLISHED,
                    {
                        "at": published_at,
                        "scope": str(item.scope.value),
                        "sentiment": str(item.sentiment.value),
                        "headline": item.headline,
                        "body": item.body,
                        "symbol": item.symbol,
                        "sector": item.sector,
                        "impact_pct": round(item.impact * 100, 2),
                    },
                )
            )

        for listener in self.tick_listeners:
            try:
                await listener(result)
            except Exception:
                log.exception("tick listener failed")
        return result

    async def _leaderboard_loop(self) -> None:
        while self._running:
            await asyncio.sleep(self.settings.leaderboard_interval_seconds)
            try:
                await self.leaderboard.recompute(self.engine.day_index)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("leaderboard recompute failed")

    async def _maintenance_loop(self) -> None:
        while self._running:
            await asyncio.sleep(MAINTENANCE_INTERVAL)
            try:
                purged = await self.auth.purge_expired_sessions()
                self.auth_limiter.prune()
                self.order_limiter.prune()
                await self.portfolios.refresh_cached_values()
                if purged:
                    log.info("purged %d expired sessions", purged)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("maintenance pass failed")

    # -- event handlers -----------------------------------------------------

    async def _on_portfolio_changed(self, event: Event) -> None:
        if event.user_id is None:
            return
        try:
            portfolio = await self.portfolios.get_portfolio(event.user_id)
            await self.achievements.evaluate(event.user_id, portfolio)
        except Exception:
            log.exception("achievement evaluation failed for user %s", event.user_id)

    # -- status -------------------------------------------------------------

    def status(self) -> dict[str, Any]:
        return {
            "uptime_seconds": round(time.time() - self.started_at, 1),
            "ticks_processed": self.ticks_processed,
            "market": self.engine.market_status(),
            "season": self.leaderboard.season_info(),
        }

    def set_market_status(self, status: MarketStatus) -> None:
        self.engine.set_status(status)
