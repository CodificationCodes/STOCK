"""Portfolio valuation and player statistics.

Everything is marked to the engine's live price at the moment of the call.
The client is sent finished numbers -- it never receives a holding and a
price and is asked to multiply them, because then a modified client could
show (and worse, act on) a different balance than the server believes in.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.orm import selectinload

from stockgame.server.config import Settings
from stockgame.server.core.errors import NotFoundError
from stockgame.server.db.models import (
    Holding,
    Portfolio,
    PortfolioSnapshot,
    Stock,
    Trade,
    User,
    WatchlistItem,
)
from stockgame.server.db.session import Database
from stockgame.shared.validation import validate_symbol

log = logging.getLogger("stockgame.portfolio")


@dataclass(slots=True)
class Valuation:
    """A portfolio priced at a moment in time."""

    user_id: int
    username: str
    cash_cents: int
    holdings_value_cents: int
    total_value_cents: int
    deposited_cents: int
    realized_pl_cents: int
    unrealized_pl_cents: int
    day_open_value_cents: int

    @property
    def total_pl_cents(self) -> int:
        return self.total_value_cents - self.deposited_cents

    @property
    def total_return_pct(self) -> float:
        if not self.deposited_cents:
            return 0.0
        return self.total_pl_cents / self.deposited_cents * 100

    @property
    def day_pl_cents(self) -> int:
        return self.total_value_cents - self.day_open_value_cents

    @property
    def day_return_pct(self) -> float:
        if not self.day_open_value_cents:
            return 0.0
        return self.day_pl_cents / self.day_open_value_cents * 100


class PortfolioService:
    def __init__(self, db: Database, settings: Settings, engine) -> None:
        self.db = db
        self.settings = settings
        self.engine = engine

    # -- single player ------------------------------------------------------

    async def get_portfolio(self, user_id: int) -> dict[str, Any]:
        """The full portfolio payload sent to the owning player."""
        prices = self.engine.prices()
        async with self.db.session() as session:
            portfolio = await session.scalar(
                select(Portfolio)
                .where(Portfolio.user_id == user_id)
                .options(selectinload(Portfolio.holdings).selectinload(Holding.stock))
            )
            if portfolio is None:
                raise NotFoundError("No portfolio for this account.")
            username = await session.scalar(select(User.username).where(User.id == user_id))

        positions: list[dict[str, Any]] = []
        holdings_value = 0
        unrealized = 0
        for holding in portfolio.holdings:
            symbol = holding.stock.symbol
            price = prices.get(symbol, holding.stock.price_cents)
            market_value = price * holding.quantity
            position_pl = market_value - holding.cost_basis_cents
            holdings_value += market_value
            unrealized += position_pl
            prev_close = holding.stock.prev_close_cents or price
            positions.append(
                {
                    "symbol": symbol,
                    "name": holding.stock.name,
                    "sector": holding.stock.sector,
                    "quantity": holding.quantity,
                    "reserved_quantity": holding.reserved_quantity,
                    "avg_cost_cents": holding.avg_cost_cents,
                    "cost_basis_cents": holding.cost_basis_cents,
                    "price_cents": price,
                    "market_value_cents": market_value,
                    "unrealized_pl_cents": position_pl,
                    "return_pct": (
                        position_pl / holding.cost_basis_cents * 100
                        if holding.cost_basis_cents
                        else 0.0
                    ),
                    "day_change_pct": (
                        (price - prev_close) / prev_close * 100 if prev_close else 0.0
                    ),
                    "weight_pct": 0.0,  # filled in below, once the total is known
                }
            )

        total_value = portfolio.cash_cents + holdings_value
        for position in positions:
            position["weight_pct"] = (
                position["market_value_cents"] / total_value * 100 if total_value else 0.0
            )
        positions.sort(key=lambda row: row["market_value_cents"], reverse=True)

        valuation = Valuation(
            user_id=user_id,
            username=username or "",
            cash_cents=portfolio.cash_cents,
            holdings_value_cents=holdings_value,
            total_value_cents=total_value,
            deposited_cents=portfolio.deposited_cents,
            realized_pl_cents=portfolio.realized_pl_cents,
            unrealized_pl_cents=unrealized,
            day_open_value_cents=portfolio.day_open_value_cents or portfolio.deposited_cents,
        )
        return {
            "username": valuation.username,
            "cash_cents": valuation.cash_cents,
            "holdings_value_cents": holdings_value,
            "total_value_cents": total_value,
            "deposited_cents": valuation.deposited_cents,
            "buying_power_cents": portfolio.cash_cents,
            "realized_pl_cents": portfolio.realized_pl_cents,
            "unrealized_pl_cents": unrealized,
            "total_pl_cents": valuation.total_pl_cents,
            "total_return_pct": valuation.total_return_pct,
            "day_pl_cents": valuation.day_pl_cents,
            "day_return_pct": valuation.day_return_pct,
            "total_trades": portfolio.total_trades,
            "winning_trades": portfolio.winning_trades,
            "losing_trades": portfolio.losing_trades,
            "positions": positions,
        }

    async def value_all(self) -> list[Valuation]:
        """Value every account in two queries -- the leaderboard's workhorse."""
        prices = self.engine.prices()
        symbol_of = self.engine.symbols_by_id
        async with self.db.session() as session:
            portfolios = (
                await session.execute(
                    select(Portfolio, User.username)
                    .join(User, Portfolio.user_id == User.id)
                    .where(User.is_active.is_(True))
                )
            ).all()
            holdings = (
                await session.execute(
                    select(
                        Holding.portfolio_id,
                        Holding.stock_id,
                        Holding.quantity,
                        Holding.cost_basis_cents,
                    )
                )
            ).all()

        by_portfolio: dict[int, tuple[int, int]] = {}
        for portfolio_id, stock_id, quantity, cost_basis in holdings:
            symbol = symbol_of.get(stock_id)
            price = prices.get(symbol) if symbol else None
            if price is None:
                continue
            value, basis = by_portfolio.get(portfolio_id, (0, 0))
            by_portfolio[portfolio_id] = (value + price * quantity, basis + cost_basis)

        results = []
        for portfolio, username in portfolios:
            value, basis = by_portfolio.get(portfolio.id, (0, 0))
            results.append(
                Valuation(
                    user_id=portfolio.user_id,
                    username=username,
                    cash_cents=portfolio.cash_cents,
                    holdings_value_cents=value,
                    total_value_cents=portfolio.cash_cents + value,
                    deposited_cents=portfolio.deposited_cents,
                    realized_pl_cents=portfolio.realized_pl_cents,
                    unrealized_pl_cents=value - basis,
                    day_open_value_cents=(
                        portfolio.day_open_value_cents or portfolio.deposited_cents
                    ),
                )
            )
        return results

    async def value_of(self, user_id: int) -> int:
        data = await self.get_portfolio(user_id)
        return int(data["total_value_cents"])

    # -- profiles -----------------------------------------------------------

    async def get_profile(self, username: str, *, viewer_id: int | None = None) -> dict[str, Any]:
        """Public profile. Deliberately omits cash, orders and session data."""
        async with self.db.session() as session:
            user = await session.scalar(
                select(User).where(User.username_lower == username.strip().lower())
            )
            if user is None or not user.is_active:
                raise NotFoundError(f"No player named {username}.")
            portfolio = await session.scalar(
                select(Portfolio)
                .where(Portfolio.user_id == user.id)
                .options(selectinload(Portfolio.holdings).selectinload(Holding.stock))
            )
            trade_count = await session.scalar(
                select(func.count()).select_from(Trade).where(Trade.user_id == user.id)
            )
            history = (
                await session.execute(
                    select(PortfolioSnapshot.day_index, PortfolioSnapshot.value_cents)
                    .where(PortfolioSnapshot.user_id == user.id)
                    .order_by(PortfolioSnapshot.day_index.desc())
                    .limit(120)
                )
            ).all()

        prices = self.engine.prices()
        holdings_value = 0
        positions = []
        for holding in portfolio.holdings if portfolio else []:
            price = prices.get(holding.stock.symbol, holding.stock.price_cents)
            value = price * holding.quantity
            holdings_value += value
            positions.append(
                {
                    "symbol": holding.stock.symbol,
                    "quantity": holding.quantity,
                    "market_value_cents": value,
                    # A rival's entry price is competitive information, so a
                    # profile shows position *size* but never cost basis.
                    "return_pct": (
                        (value - holding.cost_basis_cents) / holding.cost_basis_cents * 100
                        if holding.cost_basis_cents
                        else 0.0
                    ),
                }
            )
        positions.sort(key=lambda row: row["market_value_cents"], reverse=True)

        total_value = (portfolio.cash_cents if portfolio else 0) + holdings_value
        deposited = portfolio.deposited_cents if portfolio else 0
        wins = portfolio.winning_trades if portfolio else 0
        losses = portfolio.losing_trades if portfolio else 0
        closed = wins + losses
        return {
            "username": user.username,
            "member_since": user.created_at.isoformat(),
            "last_seen": user.last_login_at.isoformat() if user.last_login_at else None,
            "total_value_cents": total_value,
            "total_pl_cents": total_value - deposited,
            "total_return_pct": ((total_value - deposited) / deposited * 100 if deposited else 0.0),
            "realized_pl_cents": portfolio.realized_pl_cents if portfolio else 0,
            "total_trades": int(trade_count or 0),
            "winning_trades": wins,
            "losing_trades": losses,
            "win_rate_pct": (wins / closed * 100) if closed else 0.0,
            "best_trade_cents": portfolio.best_trade_cents if portfolio else 0,
            "worst_trade_cents": portfolio.worst_trade_cents if portfolio else 0,
            "positions": positions[:15],
            "position_count": len(positions),
            "equity_curve": [
                {"day": day, "value_cents": value} for day, value in reversed(history)
            ],
            "is_self": viewer_id == user.id,
        }

    # -- day rollover -------------------------------------------------------

    async def snapshot_day(self, day_index: int) -> int:
        """Write the equity curve point and reset each player's daily baseline."""
        valuations = await self.value_all()
        if not valuations:
            return 0
        async with self.db.write_session() as session:
            # Clear any partial row for this day first, so a restart mid-day
            # re-snapshots cleanly. Portable across backends -- no dialect
            # specific upsert.
            await session.execute(
                delete(PortfolioSnapshot).where(PortfolioSnapshot.day_index == day_index)
            )
            session.add_all(
                PortfolioSnapshot(
                    user_id=valuation.user_id,
                    day_index=day_index,
                    value_cents=valuation.total_value_cents,
                    cash_cents=valuation.cash_cents,
                )
                for valuation in valuations
            )
            for valuation in valuations:
                await session.execute(
                    Portfolio.__table__.update()
                    .where(Portfolio.user_id == valuation.user_id)
                    .values(
                        last_value_cents=valuation.total_value_cents,
                        day_open_value_cents=valuation.total_value_cents,
                    )
                )
        log.info("snapshotted %d portfolios for day %d", len(valuations), day_index)
        return len(valuations)

    async def refresh_cached_values(self) -> None:
        """Keep ``last_value_cents`` roughly current for admin/offline queries."""
        valuations = await self.value_all()
        if not valuations:
            return
        async with self.db.write_session() as session:
            for valuation in valuations:
                await session.execute(
                    Portfolio.__table__.update()
                    .where(Portfolio.user_id == valuation.user_id)
                    .values(last_value_cents=valuation.total_value_cents)
                )

    # -- watchlist ----------------------------------------------------------

    async def get_watchlist(self, user_id: int) -> list[dict[str, Any]]:
        prices = self.engine.prices()
        async with self.db.session() as session:
            items = (
                (
                    await session.execute(
                        select(WatchlistItem)
                        .where(WatchlistItem.user_id == user_id)
                        .order_by(WatchlistItem.sort_order, WatchlistItem.id)
                        .options(selectinload(WatchlistItem.stock))
                    )
                )
                .scalars()
                .all()
            )

        rows = []
        for item in items:
            stock = item.stock
            price = prices.get(stock.symbol, stock.price_cents)
            prev = stock.prev_close_cents or price
            rows.append(
                {
                    "symbol": stock.symbol,
                    "name": stock.name,
                    "sector": stock.sector,
                    "price_cents": price,
                    "change_pct": (price - prev) / prev * 100 if prev else 0.0,
                    "day_high_cents": stock.day_high_cents,
                    "day_low_cents": stock.day_low_cents,
                }
            )
        return rows

    async def add_to_watchlist(self, user_id: int, symbol: str) -> list[dict[str, Any]]:
        symbol = validate_symbol(symbol)
        stock_id = self.engine.stock_ids.get(symbol)
        if stock_id is None:
            raise NotFoundError(f"Unknown symbol: {symbol}")
        async with self.db.write_session() as session:
            existing = await session.scalar(
                select(WatchlistItem.id).where(
                    WatchlistItem.user_id == user_id, WatchlistItem.stock_id == stock_id
                )
            )
            if existing is None:
                count = await session.scalar(
                    select(func.count())
                    .select_from(WatchlistItem)
                    .where(WatchlistItem.user_id == user_id)
                )
                session.add(
                    WatchlistItem(user_id=user_id, stock_id=stock_id, sort_order=int(count or 0))
                )
        return await self.get_watchlist(user_id)

    async def remove_from_watchlist(self, user_id: int, symbol: str) -> list[dict[str, Any]]:
        symbol = validate_symbol(symbol)
        stock_id = self.engine.stock_ids.get(symbol)
        if stock_id is not None:
            async with self.db.write_session() as session:
                await session.execute(
                    delete(WatchlistItem).where(
                        WatchlistItem.user_id == user_id, WatchlistItem.stock_id == stock_id
                    )
                )
        return await self.get_watchlist(user_id)

    async def stock_detail(self, symbol: str) -> dict[str, Any]:
        """Everything the stock page shows, other than candles."""
        symbol = validate_symbol(symbol)
        sim = self.engine.sims.get(symbol)
        if sim is None:
            raise NotFoundError(f"Unknown symbol: {symbol}")
        async with self.db.session() as session:
            stock = await session.scalar(select(Stock).where(Stock.symbol == symbol))
        day = self.engine.day_bar(symbol)
        prev = day.prev_close_cents or sim.price_cents
        return {
            "symbol": symbol,
            "name": stock.name,
            "sector": stock.sector,
            "description": stock.description,
            "price_cents": sim.price_cents,
            "prev_close_cents": prev,
            "open_cents": day.open_cents,
            "day_high_cents": day.high_cents,
            "day_low_cents": day.low_cents,
            "change_cents": sim.price_cents - prev,
            "change_pct": (sim.price_cents - prev) / prev * 100 if prev else 0.0,
            "volume": sim.day_volume,
            "market_cap_cents": sim.price_cents * stock.shares_outstanding,
            "shares_outstanding": stock.shares_outstanding,
            "volatility": sim.volatility,
            "liquidity": sim.liquidity,
            "beta": sim.beta,
            "is_halted": sim.is_halted,
        }
