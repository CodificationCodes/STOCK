"""Rankings and seasons.

Five boards are maintained. All-time and season rank on total return since
the account (or season) began; daily/weekly/monthly rank on the change since
the relevant :class:`PortfolioSnapshot` baseline, so a player who joined
yesterday competes fairly with one who joined last month.

Rankings are materialised on a timer rather than computed per request: with
every connected client watching the same board, recomputing on demand would
mean the same query N times a second.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import delete, select

from stockgame.server.config import Settings
from stockgame.server.core.events import Event, EventBus, Topics
from stockgame.server.db.models import LeaderboardEntry, PortfolioSnapshot, Season
from stockgame.server.db.session import Database
from stockgame.server.services.portfolio import PortfolioService, Valuation
from stockgame.shared.enums import LeaderboardPeriod

log = logging.getLogger("stockgame.leaderboard")

#: Simulated days used as the lookback for each rolling board.
PERIOD_LOOKBACK_DAYS = {
    LeaderboardPeriod.DAILY: 1,
    LeaderboardPeriod.WEEKLY: 5,
    LeaderboardPeriod.MONTHLY: 21,
}

SEASON_NAMES = (
    "The Opening Bell",
    "Bull Run",
    "Circuit Breaker",
    "Short Squeeze",
    "Dead Cat Bounce",
    "Melt Up",
    "Flight to Quality",
    "Animal Spirits",
)


class LeaderboardService:
    def __init__(
        self,
        db: Database,
        settings: Settings,
        portfolios: PortfolioService,
        bus: EventBus,
    ) -> None:
        self.db = db
        self.settings = settings
        self.portfolios = portfolios
        self.bus = bus
        self._cache: dict[str, list[dict[str, Any]]] = {}
        self._computed_at: datetime | None = None
        self.season: Season | None = None

    # -- seasons ------------------------------------------------------------

    async def ensure_season(self) -> Season:
        """Return the running season, starting or rolling one over as needed.

        Only competitive *rankings* reset at a season boundary. Cash,
        holdings and history are never touched.
        """
        now = datetime.now(timezone.utc)
        async with self.db.write_session() as session:
            season = await session.scalar(
                select(Season).where(Season.is_active.is_(True)).order_by(Season.number.desc())
            )
            if season is not None and season.ends_at and season.ends_at <= now:
                season.is_active = False
                log.info("Season %d (%s) has ended", season.number, season.name)
                season = None
            if season is None:
                previous = await session.scalar(
                    select(Season).order_by(Season.number.desc()).limit(1)
                )
                number = (previous.number + 1) if previous else 1
                season = Season(
                    number=number,
                    name=SEASON_NAMES[(number - 1) % len(SEASON_NAMES)],
                    started_at=now,
                    ends_at=now + timedelta(days=self.settings.season_length_days),
                    is_active=True,
                )
                session.add(season)
                await session.flush()
                log.info("Season %d begins: %s", season.number, season.name)
            session.expunge(season)
        self.season = season
        return season

    def season_info(self) -> dict[str, Any]:
        if self.season is None:
            return {}
        return {
            "number": self.season.number,
            "name": self.season.name,
            "started_at": self.season.started_at.isoformat(),
            "ends_at": self.season.ends_at.isoformat() if self.season.ends_at else None,
            "days_remaining": (
                max(0, (self.season.ends_at - datetime.now(timezone.utc)).days)
                if self.season.ends_at
                else None
            ),
        }

    # -- ranking ------------------------------------------------------------

    async def recompute(self, day_index: int) -> dict[str, list[dict[str, Any]]]:
        """Rebuild every board and persist the materialised rows."""
        season = self.season or await self.ensure_season()
        valuations = await self.portfolios.value_all()
        if not valuations:
            self._cache = {}
            return {}

        baselines = await self._baselines(day_index)
        boards: dict[str, list[dict[str, Any]]] = {}

        boards[str(LeaderboardPeriod.ALL_TIME.value)] = self._rank(
            valuations,
            lambda v: (v.total_pl_cents, v.total_return_pct),
        )
        boards[str(LeaderboardPeriod.SEASON.value)] = self._rank(
            valuations,
            lambda v: self._delta(v, baselines.get("season", {}).get(v.user_id)),
        )
        for period, lookback in PERIOD_LOOKBACK_DAYS.items():
            key = str(period.value)
            boards[key] = self._rank(
                valuations,
                lambda v, lb=lookback: self._delta(v, baselines.get(lb, {}).get(v.user_id)),
            )

        await self._persist(boards, season)
        self._cache = boards
        self._computed_at = datetime.now(timezone.utc)
        await self.bus.publish(Event(Topics.LEADERBOARD_UPDATED, {"periods": list(boards)}))
        return boards

    def _delta(self, valuation: Valuation, baseline: int | None) -> tuple[int, float]:
        """Profit and return relative to a baseline value."""
        start = baseline if baseline is not None else valuation.deposited_cents
        if start <= 0:
            start = valuation.deposited_cents or 1
        profit = valuation.total_value_cents - start
        return profit, profit / start * 100

    def _rank(self, valuations: list[Valuation], key) -> list[dict[str, Any]]:
        scored = []
        for valuation in valuations:
            profit, pct = key(valuation)
            scored.append((valuation, profit, pct))
        # Ranked on percentage return, so a player who started later is not
        # penalised for having had less time to compound absolute dollars.
        scored.sort(key=lambda row: (row[2], row[1]), reverse=True)
        return [
            {
                "rank": index + 1,
                "user_id": valuation.user_id,
                "username": valuation.username,
                "value_cents": valuation.total_value_cents,
                "pl_cents": profit,
                "return_pct": round(pct, 3),
                "cash_cents": None,  # never exposed on a public board
            }
            for index, (valuation, profit, pct) in enumerate(
                scored[: self.settings.leaderboard_size]
            )
        ]

    async def _baselines(self, day_index: int) -> dict[Any, dict[int, int]]:
        """Portfolio values to measure each rolling board against."""
        result: dict[Any, dict[int, int]] = {}
        async with self.db.session() as session:
            for lookback in PERIOD_LOOKBACK_DAYS.values():
                target = max(0, day_index - lookback)
                rows = (
                    await session.execute(
                        select(PortfolioSnapshot.user_id, PortfolioSnapshot.value_cents).where(
                            PortfolioSnapshot.day_index == target
                        )
                    )
                ).all()
                result[lookback] = dict(rows)

            season_start_day = None
            if self.season is not None:
                row = await session.scalar(
                    select(PortfolioSnapshot.day_index)
                    .where(PortfolioSnapshot.created_at >= self.season.started_at)
                    .order_by(PortfolioSnapshot.day_index)
                    .limit(1)
                )
                season_start_day = row
            if season_start_day is not None:
                rows = (
                    await session.execute(
                        select(PortfolioSnapshot.user_id, PortfolioSnapshot.value_cents).where(
                            PortfolioSnapshot.day_index == season_start_day
                        )
                    )
                ).all()
                result["season"] = dict(rows)
            else:
                result["season"] = {}
        return result

    async def _persist(self, boards: dict[str, list[dict[str, Any]]], season: Season) -> None:
        async with self.db.write_session() as session:
            await session.execute(delete(LeaderboardEntry))
            for period, rows in boards.items():
                for row in rows:
                    session.add(
                        LeaderboardEntry(
                            period=period,
                            season_id=season.id
                            if period in (str(LeaderboardPeriod.SEASON.value),)
                            else None,
                            user_id=row["user_id"],
                            username=row["username"],
                            rank=row["rank"],
                            value_cents=row["value_cents"],
                            pl_cents=row["pl_cents"],
                            return_pct=row["return_pct"],
                        )
                    )

    # -- reads --------------------------------------------------------------

    def board(self, period: str | LeaderboardPeriod, limit: int = 50) -> list[dict[str, Any]]:
        key = str(period.value if isinstance(period, LeaderboardPeriod) else period)
        return self._cache.get(key, [])[:limit]

    def all_boards(self, limit: int = 50) -> dict[str, list[dict[str, Any]]]:
        return {period: rows[:limit] for period, rows in self._cache.items()}

    def rank_of(self, user_id: int, period: str = "all_time") -> int | None:
        for row in self._cache.get(period, []):
            if row["user_id"] == user_id:
                return int(row["rank"])
        return None

    def player_count(self) -> int:
        return len(self._cache.get(str(LeaderboardPeriod.ALL_TIME.value), []))

    def payload(self, limit: int = 50) -> dict[str, Any]:
        return {
            "season": self.season_info(),
            "boards": self.all_boards(limit),
            "computed_at": self._computed_at.isoformat() if self._computed_at else None,
        }
