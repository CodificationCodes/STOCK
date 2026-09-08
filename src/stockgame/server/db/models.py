"""ORM models -- the authoritative state of the game world.

Money is stored as integer cents (``*_cents``) so no balance ever suffers
floating-point drift. Share quantities are whole integers. Simulation
parameters live on the row alongside the price so a server restart resumes
the market exactly where it left off.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from stockgame.server.db.base import Base, UTCDateTime, utcnow


class User(Base):
    """An account. Holds credentials only -- money lives on :class:`Portfolio`."""

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(20), nullable=False)
    #: Case-folded copy so "Trader" and "trader" cannot both be registered.
    username_lower: Mapped[str] = mapped_column(String(20), unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)
    last_login_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    #: Incremented on password change to invalidate every outstanding session.
    token_epoch: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    portfolio: Mapped[Portfolio] = relationship(
        back_populates="user", uselist=False, cascade="all, delete-orphan"
    )
    sessions: Mapped[list[Session]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )


class Session(Base):
    """A bearer token. Only the SHA-256 of the token is persisted."""

    __tablename__ = "sessions"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False, index=True)
    last_seen_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)
    revoked: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    client_info: Mapped[str | None] = mapped_column(String(120))

    user: Mapped[User] = relationship(back_populates="sessions")


class Portfolio(Base):
    """Per-player financial state. One row per user, created at registration."""

    __tablename__ = "portfolios"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), unique=True, nullable=False
    )
    cash_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    #: What the account was funded with; the denominator of total return.
    deposited_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    realized_pl_cents: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    #: Cached mark-to-market value, refreshed by the valuation service.
    last_value_cents: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    #: Portfolio value at the start of the current simulated day, for "today's P/L".
    day_open_value_cents: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    total_trades: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    winning_trades: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    losing_trades: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    best_trade_cents: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    worst_trade_cents: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)

    user: Mapped[User] = relationship(back_populates="portfolio")
    holdings: Mapped[list[Holding]] = relationship(
        back_populates="portfolio", cascade="all, delete-orphan"
    )

    __table_args__ = (CheckConstraint("cash_cents >= 0", name="cash_non_negative"),)


class Stock(Base):
    """A listed company plus its live simulation state."""

    __tablename__ = "stocks"

    id: Mapped[int] = mapped_column(primary_key=True)
    symbol: Mapped[str] = mapped_column(String(6), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    sector: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)

    price_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    prev_close_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    open_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    day_high_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    day_low_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    day_volume: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    shares_outstanding: Mapped[int] = mapped_column(Integer, nullable=False)

    # -- Simulation parameters (persisted so restarts are seamless) ----------
    #: Annualised volatility, e.g. 0.45 for a jumpy small-cap.
    volatility: Mapped[float] = mapped_column(Float, nullable=False)
    #: Long-run drift per year (the company's underlying quality).
    drift: Mapped[float] = mapped_column(Float, nullable=False)
    #: Sensitivity to market-wide moves.
    beta: Mapped[float] = mapped_column(Float, nullable=False)
    #: How much size the book absorbs before the price moves; scales slippage.
    liquidity: Mapped[float] = mapped_column(Float, nullable=False)
    #: Short-term trend carried between ticks (decays each tick).
    momentum: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    #: Pull back towards ``fair_value_cents``.
    mean_reversion: Mapped[float] = mapped_column(Float, nullable=False)
    #: Slowly-evolving anchor the price reverts to; news shifts it.
    fair_value_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    #: Decaying shock applied by news events.
    event_impact: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    is_halted: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    listed_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)

    @property
    def market_cap_cents(self) -> int:
        return self.price_cents * self.shares_outstanding


class Candle(Base):
    """OHLCV bar. ``interval`` is ``'i'`` (intraday) or ``'d'`` (end of day)."""

    __tablename__ = "price_history"

    id: Mapped[int] = mapped_column(primary_key=True)
    stock_id: Mapped[int] = mapped_column(
        ForeignKey("stocks.id", ondelete="CASCADE"), nullable=False
    )
    interval: Mapped[str] = mapped_column(String(2), nullable=False)
    #: Simulated day index this bar belongs to; monotonic across the world.
    day_index: Mapped[int] = mapped_column(Integer, nullable=False)
    ts: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    open_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    high_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    low_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    close_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    volume: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    __table_args__ = (Index("ix_price_history_lookup", "stock_id", "interval", "ts"),)


class Holding(Base):
    """A position. ``cost_basis_cents`` is the *total* paid for the open shares."""

    __tablename__ = "holdings"

    id: Mapped[int] = mapped_column(primary_key=True)
    portfolio_id: Mapped[int] = mapped_column(
        ForeignKey("portfolios.id", ondelete="CASCADE"), nullable=False
    )
    stock_id: Mapped[int] = mapped_column(
        ForeignKey("stocks.id", ondelete="CASCADE"), nullable=False
    )
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    cost_basis_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    #: Shares committed to resting sell orders; not available to sell again.
    reserved_quantity: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    first_bought_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime, default=utcnow, onupdate=utcnow, nullable=False
    )

    portfolio: Mapped[Portfolio] = relationship(back_populates="holdings")
    stock: Mapped[Stock] = relationship(lazy="joined")

    __table_args__ = (
        UniqueConstraint("portfolio_id", "stock_id", name="uq_holding"),
        CheckConstraint("quantity >= 0", name="quantity_non_negative"),
    )

    @property
    def avg_cost_cents(self) -> int:
        return self.cost_basis_cents // self.quantity if self.quantity else 0


class Order(Base):
    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(primary_key=True)
    #: Public opaque identifier; the integer PK is never exposed to clients.
    public_id: Mapped[str] = mapped_column(String(22), unique=True, nullable=False)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    stock_id: Mapped[int] = mapped_column(
        ForeignKey("stocks.id", ondelete="CASCADE"), nullable=False, index=True
    )
    side: Mapped[str] = mapped_column(String(8), nullable=False)
    order_type: Mapped[str] = mapped_column(String(12), nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    filled_quantity: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    limit_price_cents: Mapped[int | None] = mapped_column(Integer)
    #: Volume-weighted average of the fills so far.
    avg_fill_cents: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    reject_reason: Mapped[str | None] = mapped_column(String(120))
    #: Cash ring-fenced for a resting buy so the player cannot double-spend it.
    reserved_cash_cents: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime, default=utcnow, onupdate=utcnow, nullable=False
    )
    #: Resting orders are cancelled at this simulated day; NULL = good til cancelled.
    expires_day: Mapped[int | None] = mapped_column(Integer)

    stock: Mapped[Stock] = relationship(lazy="joined")

    __table_args__ = (
        Index("ix_orders_open", "status", "stock_id"),
        CheckConstraint("quantity > 0", name="quantity_positive"),
    )

    @property
    def remaining(self) -> int:
        return self.quantity - self.filled_quantity


class Trade(Base):
    """An executed fill. Append-only; this is the audit trail."""

    __tablename__ = "trades"

    id: Mapped[int] = mapped_column(primary_key=True)
    public_id: Mapped[str] = mapped_column(String(22), unique=True, nullable=False)
    order_id: Mapped[int] = mapped_column(
        ForeignKey("orders.id", ondelete="CASCADE"), nullable=False, index=True
    )
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    stock_id: Mapped[int] = mapped_column(
        ForeignKey("stocks.id", ondelete="CASCADE"), nullable=False, index=True
    )
    side: Mapped[str] = mapped_column(String(8), nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    price_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    commission_cents: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    #: Realised profit on a sell (0 for buys).
    realized_pl_cents: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    day_index: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime, default=utcnow, nullable=False, index=True
    )

    stock: Mapped[Stock] = relationship(lazy="joined")

    @property
    def gross_cents(self) -> int:
        return self.quantity * self.price_cents


class MarketEvent(Base):
    """A news headline and the shock it applied to the market."""

    __tablename__ = "market_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    scope: Mapped[str] = mapped_column(String(12), nullable=False)
    sentiment: Mapped[str] = mapped_column(String(10), nullable=False)
    headline: Mapped[str] = mapped_column(String(160), nullable=False)
    body: Mapped[str] = mapped_column(Text, default="", nullable=False)
    stock_id: Mapped[int | None] = mapped_column(ForeignKey("stocks.id", ondelete="SET NULL"))
    symbol: Mapped[str | None] = mapped_column(String(6))
    sector: Mapped[str | None] = mapped_column(String(32))
    #: Instantaneous price shock applied, as a fraction (0.087 == +8.7%).
    impact: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    day_index: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime, default=utcnow, nullable=False, index=True
    )


class MarketState(Base):
    """Singleton row (id=1) describing the shared world clock and mood."""

    __tablename__ = "market_state"

    id: Mapped[int] = mapped_column(primary_key=True)
    status: Mapped[str] = mapped_column(String(10), default="open", nullable=False)
    regime: Mapped[str] = mapped_column(String(10), default="neutral", nullable=False)
    #: Ticks remaining before the regime is re-rolled.
    regime_ticks_left: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    day_index: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    day_started_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)
    tick_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    #: Ticks accumulated into the intraday candle currently being built.
    candle_ticks: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    index_value: Mapped[int] = mapped_column(Integer, default=100_000, nullable=False)
    index_open_cents: Mapped[int] = mapped_column(Integer, default=100_000, nullable=False)
    season_id: Mapped[int | None] = mapped_column(ForeignKey("seasons.id", ondelete="SET NULL"))
    seeded: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime, default=utcnow, onupdate=utcnow, nullable=False
    )


class Season(Base):
    """A competitive period. Rankings reset; accounts and history never do."""

    __tablename__ = "seasons"

    id: Mapped[int] = mapped_column(primary_key=True)
    number: Mapped[int] = mapped_column(Integer, unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(60), nullable=False)
    started_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)
    ends_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class LeaderboardEntry(Base):
    """Materialised ranking rows, recomputed on a timer and pushed to clients."""

    __tablename__ = "leaderboard_entries"

    id: Mapped[int] = mapped_column(primary_key=True)
    period: Mapped[str] = mapped_column(String(12), nullable=False)
    season_id: Mapped[int | None] = mapped_column(ForeignKey("seasons.id", ondelete="CASCADE"))
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    username: Mapped[str] = mapped_column(String(20), nullable=False)
    rank: Mapped[int] = mapped_column(Integer, nullable=False)
    value_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    pl_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    return_pct: Mapped[float] = mapped_column(Float, nullable=False)
    total_trades: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    computed_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)

    __table_args__ = (
        UniqueConstraint("period", "season_id", "user_id", name="uq_leaderboard_row"),
        Index("ix_leaderboard_period_rank", "period", "rank"),
    )


class PortfolioSnapshot(Base):
    """Equity curve sample, written at each simulated day close.

    Also provides the baselines the daily/weekly/monthly leaderboards
    difference against.
    """

    __tablename__ = "portfolio_snapshots"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    day_index: Mapped[int] = mapped_column(Integer, nullable=False)
    value_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    cash_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)

    __table_args__ = (
        UniqueConstraint("user_id", "day_index", name="uq_snapshot_day"),
        Index("ix_snapshot_user_day", "user_id", "day_index"),
    )


class WatchlistItem(Base):
    __tablename__ = "watchlist_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    stock_id: Mapped[int] = mapped_column(
        ForeignKey("stocks.id", ondelete="CASCADE"), nullable=False
    )
    sort_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)

    stock: Mapped[Stock] = relationship(lazy="joined")

    __table_args__ = (UniqueConstraint("user_id", "stock_id", name="uq_watchlist_item"),)


class Achievement(Base):
    """Unlocked badge. Definitions live in code; only unlocks are persisted."""

    __tablename__ = "achievements"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    code: Mapped[str] = mapped_column(String(40), nullable=False)
    unlocked_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)

    __table_args__ = (UniqueConstraint("user_id", "code", name="uq_achievement"),)
