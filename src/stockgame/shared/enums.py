"""Enumerations shared between client and server.

These are plain ``str`` enums so they serialise straight to JSON and compare
cleanly against raw strings arriving over the wire.
"""

from __future__ import annotations

from enum import Enum


class StrEnum(str, Enum):
    """``str``-backed enum with a readable ``str()`` (stdlib StrEnum needs 3.11)."""

    def __str__(self) -> str:  # pragma: no cover - trivial
        return str(self.value)


class Sector(StrEnum):
    TECHNOLOGY = "Technology"
    ENERGY = "Energy"
    HEALTHCARE = "Healthcare"
    FINANCE = "Finance"
    CONSUMER = "Consumer"
    INDUSTRIAL = "Industrial"
    MINING = "Mining"
    COMMUNICATIONS = "Communications"


class OrderSide(StrEnum):
    BUY = "buy"
    SELL = "sell"


class OrderType(StrEnum):
    MARKET = "market"
    LIMIT = "limit"
    # Reserved for future execution types; the order pipeline already routes on
    # this value so adding them is a matter of implementing a trigger rule.
    STOP = "stop"
    STOP_LIMIT = "stop_limit"


class OrderStatus(StrEnum):
    PENDING = "pending"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCELLED = "cancelled"
    REJECTED = "rejected"
    EXPIRED = "expired"

    @property
    def is_terminal(self) -> bool:
        return self in _TERMINAL_STATUSES


_TERMINAL_STATUSES = frozenset(
    {
        OrderStatus.FILLED,
        OrderStatus.CANCELLED,
        OrderStatus.REJECTED,
        OrderStatus.EXPIRED,
    }
)


class MarketRegime(StrEnum):
    """Slow-moving market-wide mood that biases drift and volatility."""

    BULL = "bull"
    NEUTRAL = "neutral"
    BEAR = "bear"
    PANIC = "panic"
    EUPHORIA = "euphoria"


class MarketStatus(StrEnum):
    OPEN = "open"
    CLOSED = "closed"
    HALTED = "halted"


class NewsScope(StrEnum):
    COMPANY = "company"
    SECTOR = "sector"
    MARKET = "market"


class NewsSentiment(StrEnum):
    POSITIVE = "positive"
    NEGATIVE = "negative"
    NEUTRAL = "neutral"


class LeaderboardPeriod(StrEnum):
    DAILY = "daily"
    WEEKLY = "weekly"
    MONTHLY = "monthly"
    SEASON = "season"
    ALL_TIME = "all_time"


class Timeframe(StrEnum):
    """Chart ranges the client can request for a symbol."""

    D1 = "1D"
    W1 = "1W"
    M1 = "1M"
    M3 = "3M"
    Y1 = "1Y"


#: Timeframes that are served from intraday (tick-aggregated) candles rather
#: than end-of-day candles.
INTRADAY_TIMEFRAMES = frozenset({Timeframe.D1})
