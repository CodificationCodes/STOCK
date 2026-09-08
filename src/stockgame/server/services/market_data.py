"""Read-side queries for charts and news.

Candles are downsampled *server-side* to the number of columns the client
asked for. A 1Y chart is ~250 daily bars but a terminal is 80 columns wide,
so sending all 250 would be three times the bytes for no extra pixels.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select

from stockgame.server.db.models import Candle, MarketEvent, Stock, Trade, User
from stockgame.server.db.session import Database
from stockgame.server.market.engine import DAILY, INTRADAY
from stockgame.shared.enums import Timeframe
from stockgame.shared.validation import validate_symbol

#: Simulated days of end-of-day history behind each timeframe.
TIMEFRAME_DAYS = {
    Timeframe.W1: 5,
    Timeframe.M1: 21,
    Timeframe.M3: 63,
    Timeframe.Y1: 252,
}
MAX_POINTS = 400


class MarketDataService:
    def __init__(self, db: Database, engine) -> None:
        self.db = db
        self.engine = engine

    async def candles(
        self, symbol: str, timeframe: str = "1D", points: int = 120
    ) -> dict[str, Any]:
        symbol = validate_symbol(symbol)
        stock_id = self.engine.stock_ids.get(symbol)
        if stock_id is None:
            raise KeyError(symbol)
        try:
            frame = Timeframe(timeframe.upper())
        except ValueError:
            frame = Timeframe.D1
        points = max(10, min(int(points or 120), MAX_POINTS))

        if frame is Timeframe.D1:
            rows = await self._intraday(stock_id, points)
        else:
            rows = await self._daily(stock_id, TIMEFRAME_DAYS[frame])

        bars = _downsample(rows, points)
        # The final bar is always the live price, so the chart's right edge
        # matches the quote in the header rather than lagging a candle behind.
        live = self.engine.price_of(symbol)
        if bars and live is not None:
            bars[-1] = {
                **bars[-1],
                "c": live,
                "h": max(bars[-1]["h"], live),
                "l": min(bars[-1]["l"], live),
            }
        return {"symbol": symbol, "timeframe": str(frame.value), "candles": bars}

    async def _intraday(self, stock_id: int, points: int) -> list[dict[str, Any]]:
        async with self.db.session() as session:
            rows = (
                (
                    await session.execute(
                        select(Candle)
                        .where(Candle.stock_id == stock_id, Candle.interval == INTRADAY)
                        .order_by(Candle.ts.desc())
                        .limit(MAX_POINTS)
                    )
                )
                .scalars()
                .all()
            )
        candles = [_row(candle) for candle in reversed(rows)]
        if len(candles) < 5:
            # A freshly seeded world has no intraday bars yet; fall back to
            # end-of-day so the 1D tab still renders something meaningful.
            return await self._daily(stock_id, 30)
        return candles

    async def _daily(self, stock_id: int, days: int) -> list[dict[str, Any]]:
        async with self.db.session() as session:
            rows = (
                (
                    await session.execute(
                        select(Candle)
                        .where(Candle.stock_id == stock_id, Candle.interval == DAILY)
                        .order_by(Candle.day_index.desc())
                        .limit(days)
                    )
                )
                .scalars()
                .all()
            )
        return [_row(candle) for candle in reversed(rows)]

    async def news(
        self, *, limit: int = 40, symbol: str | None = None, scope: str | None = None
    ) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit or 40), 200))
        query = select(MarketEvent).order_by(MarketEvent.created_at.desc()).limit(limit)
        if symbol:
            query = query.where(MarketEvent.symbol == validate_symbol(symbol))
        if scope:
            query = query.where(MarketEvent.scope == scope)
        async with self.db.session() as session:
            rows = (await session.execute(query)).scalars().all()
        return [
            {
                "id": row.id,
                "scope": row.scope,
                "sentiment": row.sentiment,
                "headline": row.headline,
                "body": row.body,
                "symbol": row.symbol,
                "sector": row.sector,
                "impact_pct": round(row.impact * 100, 2),
                "at": row.created_at.isoformat(),
            }
            for row in rows
        ]

    async def recent_tape(self, limit: int = 25) -> list[dict[str, Any]]:
        """The last few fills across all players, for the live tape panel.

        Anonymised in the same way as the live push: username, symbol, side,
        size and price -- never anyone's balance or profit.
        """
        limit = max(1, min(int(limit or 25), 100))
        async with self.db.session() as session:
            rows = (
                await session.execute(
                    select(Trade, Stock.symbol, User.username)
                    .join(Stock, Stock.id == Trade.stock_id)
                    .join(User, User.id == Trade.user_id)
                    .order_by(Trade.created_at.desc())
                    .limit(limit)
                )
            ).all()
        return [
            {
                "username": username,
                "symbol": symbol,
                "side": trade.side,
                "quantity": trade.quantity,
                "price_cents": trade.price_cents,
                "at": trade.created_at.isoformat(),
            }
            for trade, symbol, username in reversed(rows)
        ]

    async def sectors(self) -> list[dict[str, Any]]:
        return self.engine.sector_performance()

    async def stock_names(self) -> dict[str, str]:
        async with self.db.session() as session:
            rows = (await session.execute(select(Stock.symbol, Stock.name))).all()
        return dict(rows)


def _row(candle: Candle) -> dict[str, Any]:
    """Short keys: this payload is sent constantly, so bytes matter."""
    return {
        "t": candle.ts.isoformat(),
        "o": candle.open_cents,
        "h": candle.high_cents,
        "l": candle.low_cents,
        "c": candle.close_cents,
        "v": candle.volume,
    }


def _downsample(rows: list[dict[str, Any]], points: int) -> list[dict[str, Any]]:
    """Aggregate into at most ``points`` bars, preserving true OHLC."""
    if len(rows) <= points:
        return rows
    bucket_size = len(rows) / points
    result: list[dict[str, Any]] = []
    for index in range(points):
        start = int(index * bucket_size)
        end = max(start + 1, int((index + 1) * bucket_size))
        chunk = rows[start:end]
        if not chunk:
            continue
        result.append(
            {
                "t": chunk[0]["t"],
                "o": chunk[0]["o"],
                "h": max(bar["h"] for bar in chunk),
                "l": min(bar["l"] for bar in chunk),
                "c": chunk[-1]["c"],
                "v": sum(bar["v"] for bar in chunk),
            }
        )
    return result
