"""Achievements.

Definitions live in code and unlocks live in the database, so new badges can
be added in a release without a migration and without retroactively awarding
them incorrectly -- each rule is evaluated against current state whenever a
player trades.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select

from stockgame.server.core.events import Event, EventBus, Topics
from stockgame.server.db.models import Achievement
from stockgame.server.db.session import Database

log = logging.getLogger("stockgame.achievements")


@dataclass(frozen=True, slots=True)
class Badge:
    code: str
    name: str
    description: str
    #: Evaluated against the portfolio payload; True means earned.
    rule: Callable[[dict[str, Any]], bool]


BADGES: tuple[Badge, ...] = (
    Badge(
        "first_trade", "Opening Bell", "Execute your first trade", lambda p: p["total_trades"] >= 1
    ),
    Badge("ten_trades", "Active Trader", "Execute 10 trades", lambda p: p["total_trades"] >= 10),
    Badge(
        "hundred_trades", "Market Maker", "Execute 100 trades", lambda p: p["total_trades"] >= 100
    ),
    Badge(
        "first_profit",
        "In The Green",
        "Close a trade at a profit",
        lambda p: p["winning_trades"] >= 1,
    ),
    Badge(
        "diversified",
        "Diversified",
        "Hold 8 or more positions at once",
        lambda p: len(p["positions"]) >= 8,
    ),
    Badge(
        "concentrated",
        "Conviction",
        "Put over 50% of your book in one name",
        lambda p: any(pos["weight_pct"] > 50 for pos in p["positions"]),
    ),
    Badge("up_10", "Up 10%", "Reach a 10% total return", lambda p: p["total_return_pct"] >= 10),
    Badge("up_25", "Up 25%", "Reach a 25% total return", lambda p: p["total_return_pct"] >= 25),
    Badge("up_50", "Half Again", "Reach a 50% total return", lambda p: p["total_return_pct"] >= 50),
    Badge(
        "double",
        "Doubled Up",
        "Double your starting capital",
        lambda p: p["total_return_pct"] >= 100,
    ),
    Badge(
        "six_figures",
        "Six Figures",
        "Bank $100,000 in realised profit",
        lambda p: p["realized_pl_cents"] >= 10_000_000,
    ),
    Badge(
        "all_in",
        "Fully Invested",
        "Deploy 95% of your capital",
        lambda p: p["total_value_cents"] > 0 and p["cash_cents"] / p["total_value_cents"] < 0.05,
    ),
)

BY_CODE = {badge.code: badge for badge in BADGES}


class AchievementService:
    def __init__(self, db: Database, bus: EventBus) -> None:
        self.db = db
        self.bus = bus

    async def evaluate(self, user_id: int, portfolio: dict[str, Any]) -> list[Badge]:
        """Award any newly-earned badges. Safe to call on every fill."""
        earned = {badge.code for badge in BADGES if _safe(badge, portfolio)}
        if not earned:
            return []

        async with self.db.write_session() as session:
            existing = set(
                (
                    await session.execute(
                        select(Achievement.code).where(Achievement.user_id == user_id)
                    )
                ).scalars()
            )
            fresh = earned - existing
            for code in fresh:
                session.add(Achievement(user_id=user_id, code=code))

        badges = [BY_CODE[code] for code in fresh]
        for badge in badges:
            log.info("achievement user=%s code=%s", user_id, badge.code)
            await self.bus.publish(
                Event(
                    Topics.ACHIEVEMENT_UNLOCKED,
                    {"code": badge.code, "name": badge.name, "description": badge.description},
                    user_id=user_id,
                )
            )
        return badges

    async def list_for(self, user_id: int) -> list[dict[str, Any]]:
        async with self.db.session() as session:
            rows = (
                await session.execute(
                    select(Achievement.code, Achievement.unlocked_at).where(
                        Achievement.user_id == user_id
                    )
                )
            ).all()
        unlocked = dict(rows)
        return [
            {
                "code": badge.code,
                "name": badge.name,
                "description": badge.description,
                "unlocked": badge.code in unlocked,
                "unlocked_at": (
                    unlocked[badge.code].isoformat() if badge.code in unlocked else None
                ),
            }
            for badge in BADGES
        ]


def _safe(badge: Badge, portfolio: dict[str, Any]) -> bool:
    """A broken badge rule must never break a trade."""
    try:
        return bool(badge.rule(portfolio))
    except Exception:  # pragma: no cover - defensive
        log.exception("achievement rule %s raised", badge.code)
        return False
