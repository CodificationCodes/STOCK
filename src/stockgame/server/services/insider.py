"""The insider desk: buy a rumour, hope it is true.

A tip costs real cash, names one stock, and advertises a move. Pay more and
the advertised move is bigger, on a saturating curve so the desk cannot be
farmed by simply writing a larger cheque. A share of tips are duds: the move
still happens, just far smaller, which makes a bad tip indistinguishable
from a good one until it lands.

The fee leaves the economy the moment it is paid. That is the only thing
keeping this from being a money printer, so it is deliberately steep.
"""

from __future__ import annotations

import logging
import random
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select

from stockgame.server.config import Settings
from stockgame.server.core.errors import (
    InsufficientFundsError,
    InvalidOrderError,
    NotFoundError,
)
from stockgame.server.core.security import public_id
from stockgame.server.db.models import InsiderTip, Portfolio
from stockgame.server.db.session import Database
from stockgame.server.market import pricing
from stockgame.shared.money import to_cents

log = logging.getLogger("stockgame.insider")


def move_for_fee(fee_cents: int, settings: Settings) -> float:
    """Advertised move for a fee, saturating towards ``insider_max_move_pct``.

    A hyperbola rather than a straight line: the first few thousand buy most
    of the edge, and the cheque after that buys progressively less.
    """
    halfway = max(1.0, to_cents(settings.insider_fee_halfway))
    strength = fee_cents / (fee_cents + halfway)
    span = settings.insider_max_move_pct - settings.insider_min_move_pct
    # strength is 0.5 at the halfway fee, so double it to span the range.
    return settings.insider_min_move_pct + span * min(1.0, strength * 2.0)


class InsiderService:
    def __init__(
        self,
        db: Database,
        settings: Settings,
        engine,
        rng: random.Random | None = None,
    ) -> None:
        self.db = db
        self.settings = settings
        self.engine = engine
        self.rng = rng or random.Random()

    # -- buying -------------------------------------------------------------

    async def buy_tip(self, user_id: int, amount: float) -> dict[str, Any]:
        if not self.settings.insider_enabled:
            raise InvalidOrderError("The insider desk is closed.")

        fee_cents = to_cents(amount)
        floor = to_cents(self.settings.insider_min_fee)
        ceiling = to_cents(self.settings.insider_max_fee)
        if fee_cents < floor:
            raise InvalidOrderError(
                f"The desk does not get out of bed for less than ${floor / 100:,.0f}."
            )
        fee_cents = min(fee_cents, ceiling)

        symbol = self._pick_symbol()
        stock_id = self.engine.stock_ids[symbol]
        promised = move_for_fee(fee_cents, self.settings)
        genuine = self.rng.random() >= self.settings.insider_dud_chance
        actual = promised if genuine else promised * self.settings.insider_dud_share
        applies_at = datetime.now(timezone.utc) + timedelta(
            seconds=max(0.0, self.settings.insider_lead_seconds)
        )

        async with self.db.write_session() as session:
            portfolio = await session.scalar(select(Portfolio).where(Portfolio.user_id == user_id))
            if portfolio is None:
                raise NotFoundError("No portfolio for this account.")
            if portfolio.cash_cents < fee_cents:
                raise InsufficientFundsError(
                    f"A tip costs ${fee_cents / 100:,.2f} and you have "
                    f"${portfolio.cash_cents / 100:,.2f}."
                )
            portfolio.cash_cents -= fee_cents
            tip = InsiderTip(
                public_id=public_id(),
                user_id=user_id,
                stock_id=stock_id,
                fee_cents=fee_cents,
                promised_pct=round(promised, 3),
                actual_pct=round(actual, 3),
                genuine=genuine,
                applies_at=applies_at,
            )
            session.add(tip)
            await session.flush()
            payload = self._payload(tip, symbol, reveal=False)

        log.info(
            "insider user=%s %s fee=%d promised=%.2f genuine=%s",
            user_id,
            symbol,
            fee_cents,
            promised,
            genuine,
        )
        return payload

    def _pick_symbol(self) -> str:
        tradeable = [symbol for symbol, sim in self.engine.sims.items() if not sim.is_halted]
        if not tradeable:
            raise InvalidOrderError("Nothing is trading right now.")
        return self.rng.choice(tradeable)

    # -- settlement ---------------------------------------------------------

    async def apply_due_tips(self) -> list[dict[str, Any]]:
        """Push the price of every tip whose lead time has run out."""
        now = datetime.now(timezone.utc)
        applied: list[dict[str, Any]] = []
        async with self.db.write_session() as session:
            due = (
                (
                    await session.execute(
                        select(InsiderTip).where(
                            InsiderTip.applied.is_(False), InsiderTip.applies_at <= now
                        )
                    )
                )
                .scalars()
                .all()
            )
            for tip in due:
                symbol = self.engine.symbols_by_id.get(tip.stock_id)
                sim = self.engine.sims.get(symbol) if symbol else None
                tip.applied = True
                if sim is None:
                    continue
                # A rumour moves the price without re-rating the business, so
                # most of it is transient and mean reversion claws it back.
                pricing.apply_shock(sim, tip.actual_pct / 100.0, fair_value_share=0.25)
                applied.append(self._payload(tip, symbol, reveal=True))
        return applied

    # -- queries ------------------------------------------------------------

    async def list_tips(self, user_id: int, limit: int = 20) -> list[dict[str, Any]]:
        async with self.db.session() as session:
            rows = (
                (
                    await session.execute(
                        select(InsiderTip)
                        .where(InsiderTip.user_id == user_id)
                        .order_by(InsiderTip.created_at.desc())
                        .limit(max(1, min(int(limit or 20), 100)))
                    )
                )
                .scalars()
                .all()
            )
            return [
                self._payload(
                    tip, self.engine.symbols_by_id.get(tip.stock_id, "?"), reveal=tip.applied
                )
                for tip in rows
            ]

    def _payload(self, tip: InsiderTip, symbol: str | None, *, reveal: bool) -> dict[str, Any]:
        payload = {
            "id": tip.public_id,
            "symbol": symbol or "?",
            "fee_cents": tip.fee_cents,
            "promised_pct": tip.promised_pct,
            "applies_at": tip.applies_at.isoformat(),
            "applied": tip.applied,
        }
        # Whether it was a dud is the answer to the bet -- only after it lands.
        if reveal:
            payload["actual_pct"] = tip.actual_pct
            payload["genuine"] = tip.genuine
        return payload
