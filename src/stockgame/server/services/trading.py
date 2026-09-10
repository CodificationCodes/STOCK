"""Order placement, execution and position keeping.

**This module is the referee.** Every balance, price, quantity and profit
figure the game reports originates here, computed from database state and the
engine's price -- never from anything the client sent. A client may ask to buy
100 shares of ACME; it may not state what ACME costs, what its cash balance
is, or what it already owns.

Invariants enforced on every path:

* ``portfolio.cash_cents`` never goes negative (also a DB check constraint).
* Cash for a resting buy is deducted at placement and refunded on cancel, so
  the same dollar cannot back two orders.
* Shares backing a resting sell are reserved on the holding, so the same
  share cannot be sold twice.
* Cost basis is tracked as a total, and reduced proportionally on a sell, so
  average-price rounding can never manufacture or destroy value.
"""

from __future__ import annotations

import logging
import random
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from stockgame.server.config import Settings
from stockgame.server.core.errors import (
    InsufficientFundsError,
    InsufficientSharesError,
    InvalidOrderError,
    MarketClosedError,
    NotFoundError,
)
from stockgame.server.core.events import Event, EventBus, Topics
from stockgame.server.core.security import public_id
from stockgame.server.db.models import Holding, Order, Portfolio, Stock, Trade, User
from stockgame.server.db.session import Database
from stockgame.server.services.execution import SimulatedVenue
from stockgame.shared.enums import MarketStatus, OrderSide, OrderStatus, OrderType
from stockgame.shared.money import to_cents
from stockgame.shared.validation import (
    ValidationError,
    validate_price,
    validate_quantity,
    validate_symbol,
)

log = logging.getLogger("stockgame.trading")

OPEN_STATUSES = (str(OrderStatus.PENDING.value), str(OrderStatus.PARTIALLY_FILLED.value))


def _reserves(order: Order) -> bool:
    """Whether this order ring-fenced cash or shares when it was placed.

    Anything that waits -- a limit order, or a market order serving out a
    settlement delay -- must, so the same dollar cannot back two orders.
    """
    return order.order_type == str(OrderType.LIMIT.value) or order.execute_after is not None


def _is_due(order: Order, now: datetime) -> bool:
    return order.execute_after is None or order.execute_after <= now


@dataclass(slots=True)
class OrderRequest:
    """A parsed, validated order request. Constructed only from raw input."""

    symbol: str
    side: OrderSide
    order_type: OrderType
    quantity: int
    limit_price_cents: int | None = None

    @classmethod
    def parse(cls, raw: dict[str, Any]) -> OrderRequest:
        symbol = validate_symbol(str(raw.get("symbol", "")))
        try:
            side = OrderSide(str(raw.get("side", "")).lower())
        except ValueError:
            raise ValidationError("Side must be 'buy' or 'sell'.") from None
        try:
            order_type = OrderType(str(raw.get("type", "market")).lower())
        except ValueError:
            raise ValidationError("Unsupported order type.") from None
        if order_type not in (OrderType.MARKET, OrderType.LIMIT):
            raise ValidationError(f"{order_type.value} orders are not supported yet.")
        quantity = validate_quantity(raw.get("quantity"))

        limit_cents: int | None = None
        if order_type is OrderType.LIMIT:
            if raw.get("limit_price") in (None, ""):
                raise ValidationError("A limit order needs a limit price.")
            limit_cents = to_cents(validate_price(raw["limit_price"]))
        return cls(symbol, side, order_type, quantity, limit_cents)


class TradingService:
    def __init__(
        self,
        db: Database,
        settings: Settings,
        engine,
        bus: EventBus,
        rng: random.Random | None = None,
    ) -> None:
        self.db = db
        self.settings = settings
        self.engine = engine
        self.bus = bus
        self.venue = SimulatedVenue(engine, rng or random.Random())
        self.commission_cents = to_cents(settings.commission)
        self._usernames: dict[int, str] = {}

    # -- placement ----------------------------------------------------------

    async def place_order(self, user_id: int, request: OrderRequest) -> dict[str, Any]:
        if self.engine.status is not MarketStatus.OPEN:
            raise MarketClosedError("The market is closed. Orders cannot be placed.")

        sim = self.engine.sims.get(request.symbol)
        if sim is None:
            raise NotFoundError(f"Unknown symbol: {request.symbol}")
        if sim.is_halted:
            raise MarketClosedError(f"Trading in {request.symbol} is halted.")

        stock_id = self.engine.stock_ids[request.symbol]

        async with self.db.write_session() as session:
            portfolio = await self._locked_portfolio(session, user_id)
            delay = max(0.0, self.settings.order_delay_seconds)
            execute_after = datetime.now(timezone.utc) + timedelta(seconds=delay) if delay else None
            order = Order(
                public_id=public_id(),
                user_id=user_id,
                stock_id=stock_id,
                side=str(request.side.value),
                order_type=str(request.order_type.value),
                quantity=request.quantity,
                limit_price_cents=request.limit_price_cents,
                status=str(OrderStatus.PENDING.value),
                execute_after=execute_after,
            )

            if request.side is OrderSide.BUY:
                await self._authorise_buy(session, portfolio, order, request, sim)
            else:
                await self._authorise_sell(session, portfolio, order, request)

            session.add(order)
            await session.flush()

            fills: list[Trade] = []
            if execute_after is not None:
                # Settling. The tick loop fills it at the price prevailing
                # once the delay is up, so news cannot be front-run.
                pass
            elif request.order_type is OrderType.MARKET:
                fills = await self._execute_market(session, portfolio, order, request)
            else:
                # A limit order that is already marketable fills straight away,
                # exactly like a real exchange -- it never rests behind the
                # price it was willing to pay.
                fills = await self._try_fill_limit(session, portfolio, order)

            payload = self._order_payload(order, request.symbol)
            trades = [self._trade_payload(trade, request.symbol) for trade in fills]
            await session.flush()

        await self._announce(user_id, payload, trades)
        return {"order": payload, "trades": trades}

    async def _authorise_buy(
        self, session, portfolio: Portfolio, order: Order, request: OrderRequest, sim
    ) -> None:
        """Reserve the cash a buy could possibly need."""
        if request.order_type is OrderType.MARKET:
            # Reserve at the quoted price plus headroom, because the price can
            # move between authorisation and fill -- further if it has to
            # survive a settlement delay.
            quote = self.venue.quote(request.symbol, OrderSide.BUY, request.quantity)
            required = quote * request.quantity + self.commission_cents
            buffer = int(required * (0.05 if order.execute_after else 0.01))
        else:
            assert request.limit_price_cents is not None
            required = request.limit_price_cents * request.quantity + self.commission_cents
            buffer = 0

        if portfolio.cash_cents < required + buffer:
            raise InsufficientFundsError(
                "Insufficient buying power: this order needs "
                f"${(required + buffer) / 100:,.2f} but you have "
                f"${portfolio.cash_cents / 100:,.2f}."
            )
        if _reserves(order):
            reserve = required + buffer
            portfolio.cash_cents -= reserve
            order.reserved_cash_cents = reserve

    async def _authorise_sell(
        self, session, portfolio: Portfolio, order: Order, request: OrderRequest
    ) -> None:
        """Reserve the shares a sell will deliver. Short selling is not enabled."""
        holding = await session.scalar(
            select(Holding).where(
                Holding.portfolio_id == portfolio.id,
                Holding.stock_id == order.stock_id,
            )
        )
        available = (holding.quantity - holding.reserved_quantity) if holding else 0
        if available < request.quantity:
            raise InsufficientSharesError(
                f"You have {available:,} share(s) of {request.symbol} available to sell "
                f"but tried to sell {request.quantity:,}."
            )
        if _reserves(order):
            holding.reserved_quantity += request.quantity

    async def _locked_portfolio(self, session, user_id: int) -> Portfolio:
        portfolio = await session.scalar(select(Portfolio).where(Portfolio.user_id == user_id))
        if portfolio is None:
            raise NotFoundError("No portfolio for this account.")
        return portfolio

    # -- execution ----------------------------------------------------------

    async def _execute_market(
        self, session, portfolio: Portfolio, order: Order, request: OrderRequest
    ) -> list[Trade]:
        fill = self.venue.fill_market(request.symbol, request.side, request.quantity)
        if fill is None:
            order.status = str(OrderStatus.REJECTED.value)
            order.reject_reason = "Symbol is not tradeable right now."
            return []

        if request.side is OrderSide.BUY:
            cost = fill.price_cents * fill.quantity + self.commission_cents
            if cost > portfolio.cash_cents:
                # The price moved against the order between authorisation and
                # fill. Rather than overdraw, shrink the order to what the
                # cash actually covers.
                affordable = max(
                    0, (portfolio.cash_cents - self.commission_cents) // fill.price_cents
                )
                if affordable <= 0:
                    order.status = str(OrderStatus.REJECTED.value)
                    order.reject_reason = "Price moved beyond your buying power."
                    return []
                fill.quantity = affordable

        trade = await self._book_fill(session, portfolio, order, fill.quantity, fill.price_cents)
        return [trade] if trade else []

    async def _try_fill_limit(self, session, portfolio: Portfolio, order: Order) -> list[Trade]:
        """Attempt to fill a resting limit order against the current price."""
        symbol = self._symbol_of(order.stock_id)
        side = OrderSide(order.side)
        assert order.limit_price_cents is not None

        allowed = min(order.remaining, self.venue.available_size(symbol))
        if allowed <= 0:
            return []
        fill = self.venue.fill_limit(symbol, side, allowed, order.limit_price_cents)
        if fill is None:
            return []
        trade = await self._book_fill(session, portfolio, order, fill.quantity, fill.price_cents)
        return [trade] if trade else []

    async def _book_fill(
        self, session, portfolio: Portfolio, order: Order, quantity: int, price_cents: int
    ) -> Trade | None:
        """Apply one fill to cash, holdings, the order and the trade log."""
        if quantity <= 0:
            return None
        side = OrderSide(order.side)
        reserved = _reserves(order)
        gross = quantity * price_cents
        commission = self.commission_cents if order.filled_quantity == 0 else 0
        realized = 0

        holding = await session.scalar(
            select(Holding).where(
                Holding.portfolio_id == portfolio.id, Holding.stock_id == order.stock_id
            )
        )

        if side is OrderSide.BUY:
            if reserved:
                # Spend from the reservation; hand back anything unused.
                reserved_per_share = order.reserved_cash_cents // max(order.quantity, 1)
                release = reserved_per_share * quantity
                order.reserved_cash_cents = max(0, order.reserved_cash_cents - release)
                refund = release - gross - commission
                if refund:
                    portfolio.cash_cents += refund
            else:
                portfolio.cash_cents -= gross + commission

            if holding is None:
                holding = Holding(
                    portfolio_id=portfolio.id,
                    stock_id=order.stock_id,
                    quantity=quantity,
                    cost_basis_cents=gross,
                )
                session.add(holding)
            else:
                holding.quantity += quantity
                holding.cost_basis_cents += gross
                holding.updated_at = datetime.now(timezone.utc)
        else:
            if holding is None or holding.quantity < quantity:
                order.status = str(OrderStatus.REJECTED.value)
                order.reject_reason = "Shares no longer available."
                return None
            # Proportional cost relief keeps the basis exact across partials.
            cost_out = holding.cost_basis_cents * quantity // holding.quantity
            realized = gross - cost_out - commission
            holding.quantity -= quantity
            holding.cost_basis_cents -= cost_out
            if reserved:
                holding.reserved_quantity = max(0, holding.reserved_quantity - quantity)
            holding.updated_at = datetime.now(timezone.utc)
            portfolio.cash_cents += gross - commission
            portfolio.realized_pl_cents += realized
            if holding.quantity == 0:
                await session.delete(holding)

        # Volume-weighted average fill price across partials.
        total_value = order.avg_fill_cents * order.filled_quantity + gross
        order.filled_quantity += quantity
        order.avg_fill_cents = total_value // order.filled_quantity
        order.status = str(
            (
                OrderStatus.FILLED
                if order.filled_quantity >= order.quantity
                else OrderStatus.PARTIALLY_FILLED
            ).value
        )
        order.updated_at = datetime.now(timezone.utc)

        portfolio.total_trades += 1
        if side is OrderSide.SELL:
            if realized > 0:
                portfolio.winning_trades += 1
                portfolio.best_trade_cents = max(portfolio.best_trade_cents, realized)
            elif realized < 0:
                portfolio.losing_trades += 1
                portfolio.worst_trade_cents = min(portfolio.worst_trade_cents, realized)

        trade = Trade(
            public_id=public_id(),
            order_id=order.id,
            user_id=order.user_id,
            stock_id=order.stock_id,
            side=order.side,
            quantity=quantity,
            price_cents=price_cents,
            commission_cents=commission,
            realized_pl_cents=realized,
            day_index=self.engine.day_index,
        )
        session.add(trade)
        await session.flush()
        log.info(
            "fill user=%s %s %s x%d @ %d realized=%d order=%s",
            order.user_id,
            order.side,
            self._symbol_of(order.stock_id),
            quantity,
            price_cents,
            realized,
            order.public_id,
        )
        return trade

    # -- resting orders -----------------------------------------------------

    async def process_resting_orders(self) -> list[dict[str, Any]]:
        """Called once per tick: fill or expire open limit orders.

        Only orders whose limit is currently crossed are even loaded, so a
        quiet tick costs one indexed query.
        """
        if self.engine.status is not MarketStatus.OPEN:
            return []

        prices = self.engine.prices()
        now = datetime.now(timezone.utc)
        crossable: list[int] = []
        async with self.db.session() as session:
            open_orders = (
                (await session.execute(select(Order).where(Order.status.in_(OPEN_STATUSES))))
                .scalars()
                .all()
            )
            for order in open_orders:
                symbol = self._symbol_of(order.stock_id)
                price = prices.get(symbol)
                if price is None or not _is_due(order, now):
                    continue
                if order.limit_price_cents is None:
                    # A market order that has finished settling.
                    crossable.append(order.id)
                elif order.side == str(OrderSide.BUY.value):
                    if price <= order.limit_price_cents:
                        crossable.append(order.id)
                elif price >= order.limit_price_cents:
                    crossable.append(order.id)

        if not crossable:
            return []

        updates: list[tuple[int, dict[str, Any], list[dict[str, Any]]]] = []
        async with self.db.write_session() as session:
            for order_id in crossable:
                order = await session.get(Order, order_id)
                if order is None or order.status not in OPEN_STATUSES:
                    continue
                portfolio = await session.scalar(
                    select(Portfolio).where(Portfolio.user_id == order.user_id)
                )
                if portfolio is None:
                    continue
                if order.limit_price_cents is None:
                    fills = await self._fill_settled_market(session, portfolio, order)
                else:
                    fills = await self._try_fill_limit(session, portfolio, order)
                # A settling order can also end rejected, which the player
                # still needs to see.
                if fills or order.status not in OPEN_STATUSES:
                    symbol = self._symbol_of(order.stock_id)
                    if order.status == str(OrderStatus.FILLED.value):
                        await self._release_remainder(session, portfolio, order)
                    updates.append(
                        (
                            order.user_id,
                            self._order_payload(order, symbol),
                            [self._trade_payload(trade, symbol) for trade in fills],
                        )
                    )

        for user_id, order_payload, trades in updates:
            await self._announce(user_id, order_payload, trades)
        return [order for _, order, _ in updates]

    async def _fill_settled_market(
        self, session, portfolio: Portfolio, order: Order
    ) -> list[Trade]:
        """Fill a market order whose settlement delay has elapsed.

        It fills at the price now, not the price when it was placed -- that is
        the whole point of the delay. The reservation was sized with headroom,
        but a big move can still outrun it, so the fill is trimmed to what the
        player can actually afford rather than overdrawing them.
        """
        symbol = self._symbol_of(order.stock_id)
        side = OrderSide(order.side)
        fill = self.venue.fill_market(symbol, side, order.remaining)
        if fill is None:
            return []

        if side is OrderSide.BUY:
            # Release the whole reservation before pricing the fill. The
            # proportional release in _book_fill assumes the fill lands near
            # the reserved price, which a settlement-window move can break;
            # paying straight from cash keeps the arithmetic honest.
            portfolio.cash_cents += order.reserved_cash_cents
            order.reserved_cash_cents = 0
            commission = self.commission_cents if order.filled_quantity == 0 else 0
            affordable = max(0, (portfolio.cash_cents - commission) // fill.price_cents)
            fill.quantity = min(fill.quantity, affordable)
            if fill.quantity <= 0:
                order.status = str(OrderStatus.REJECTED.value)
                order.reject_reason = "Price moved beyond your buying power while settling."
                order.updated_at = datetime.now(timezone.utc)
                await self._release_remainder(session, portfolio, order)
                return []

        trade = await self._book_fill(session, portfolio, order, fill.quantity, fill.price_cents)
        return [trade] if trade else []

    async def _release_remainder(self, session, portfolio: Portfolio, order: Order) -> None:
        """Return any over-reservation once an order is fully done."""
        if order.reserved_cash_cents:
            portfolio.cash_cents += order.reserved_cash_cents
            order.reserved_cash_cents = 0

    async def cancel_order(self, user_id: int, order_public_id: str) -> dict[str, Any]:
        async with self.db.write_session() as session:
            order = await session.scalar(select(Order).where(Order.public_id == order_public_id))
            if order is None or order.user_id != user_id:
                # Same error for "not yours" and "does not exist": a player
                # must not be able to probe for other people's order ids.
                raise NotFoundError("Order not found.")
            if order.status not in OPEN_STATUSES:
                raise InvalidOrderError(f"Order is already {order.status}.")

            portfolio = await self._locked_portfolio(session, user_id)
            if order.reserved_cash_cents:
                portfolio.cash_cents += order.reserved_cash_cents
                order.reserved_cash_cents = 0
            if order.side == str(OrderSide.SELL.value):
                holding = await session.scalar(
                    select(Holding).where(
                        Holding.portfolio_id == portfolio.id,
                        Holding.stock_id == order.stock_id,
                    )
                )
                if holding is not None:
                    holding.reserved_quantity = max(0, holding.reserved_quantity - order.remaining)
            order.status = str(OrderStatus.CANCELLED.value)
            order.updated_at = datetime.now(timezone.utc)
            payload = self._order_payload(order, self._symbol_of(order.stock_id))

        await self._announce(user_id, payload, [])
        return payload

    # -- queries ------------------------------------------------------------

    async def list_orders(self, user_id: int, *, open_only: bool = False, limit: int = 100):
        async with self.db.session() as session:
            query = (
                select(Order)
                .where(Order.user_id == user_id)
                .order_by(Order.created_at.desc())
                .limit(min(limit, 500))
                .options(selectinload(Order.stock))
            )
            if open_only:
                query = query.where(Order.status.in_(OPEN_STATUSES))
            orders = (await session.execute(query)).scalars().all()
            return [self._order_payload(order, order.stock.symbol) for order in orders]

    async def list_trades(self, user_id: int, *, limit: int = 100, symbol: str | None = None):
        async with self.db.session() as session:
            query = (
                select(Trade)
                .where(Trade.user_id == user_id)
                .order_by(Trade.created_at.desc())
                .limit(min(limit, 500))
                .options(selectinload(Trade.stock))
            )
            if symbol:
                stock_id = self.engine.stock_ids.get(validate_symbol(symbol))
                query = query.where(Trade.stock_id == (stock_id or -1))
            trades = (await session.execute(query)).scalars().all()
            return [self._trade_payload(trade, trade.stock.symbol) for trade in trades]

    async def trade_count(self) -> int:
        async with self.db.session() as session:
            return int(await session.scalar(select(func.count()).select_from(Trade)) or 0)

    # -- helpers ------------------------------------------------------------

    def _symbol_of(self, stock_id: int) -> str:
        return self.engine.symbols_by_id.get(stock_id, "?")

    async def _username(self, user_id: int) -> str:
        """Cached id -> username, for the public trade tape."""
        cached = self._usernames.get(user_id)
        if cached is not None:
            return cached
        async with self.db.session() as session:
            name = await session.scalar(select(User.username).where(User.id == user_id))
        self._usernames[user_id] = name or "player"
        return self._usernames[user_id]

    def _order_payload(self, order: Order, symbol: str) -> dict[str, Any]:
        return {
            "id": order.public_id,
            "symbol": symbol,
            "side": order.side,
            "type": order.order_type,
            "quantity": order.quantity,
            "filled_quantity": order.filled_quantity,
            "limit_price_cents": order.limit_price_cents,
            "avg_fill_cents": order.avg_fill_cents,
            "status": order.status,
            "reject_reason": order.reject_reason,
            "created_at": order.created_at.isoformat(),
            "updated_at": order.updated_at.isoformat() if order.updated_at else None,
            "execute_after": order.execute_after.isoformat() if order.execute_after else None,
        }

    def _trade_payload(self, trade: Trade, symbol: str) -> dict[str, Any]:
        return {
            "id": trade.public_id,
            "symbol": symbol,
            "side": trade.side,
            "quantity": trade.quantity,
            "price_cents": trade.price_cents,
            "value_cents": trade.quantity * trade.price_cents,
            "commission_cents": trade.commission_cents,
            "realized_pl_cents": trade.realized_pl_cents,
            "created_at": trade.created_at.isoformat(),
        }

    async def _announce(
        self, user_id: int, order_payload: dict[str, Any], trades: list[dict[str, Any]]
    ) -> None:
        await self.bus.publish(
            Event(Topics.ORDER_UPDATED, {"order": order_payload}, user_id=user_id)
        )
        if not trades:
            return
        for trade in trades:
            await self.bus.publish(Event(Topics.TRADE_EXECUTED, trade, user_id=user_id))
        await self.bus.publish(Event(Topics.PORTFOLIO_CHANGED, {}, user_id=user_id))

        # The public tape: everyone sees that a trade happened and who did it,
        # but never the size of anyone's account or their P/L on it.
        username = await self._username(user_id)
        for trade in trades:
            await self.bus.publish(
                Event(
                    Topics.TAPE_PRINTED,
                    {
                        "username": username,
                        "symbol": trade["symbol"],
                        "side": trade["side"],
                        "quantity": trade["quantity"],
                        "price_cents": trade["price_cents"],
                        "at": trade["created_at"],
                    },
                )
            )

    def _stock_id(self, symbol: str) -> int:
        stock_id = self.engine.stock_ids.get(symbol)
        if stock_id is None:
            raise NotFoundError(f"Unknown symbol: {symbol}")
        return stock_id

    async def stock_row(self, session, symbol: str) -> Stock:
        stock = await session.scalar(select(Stock).where(Stock.symbol == symbol))
        if stock is None:
            raise NotFoundError(f"Unknown symbol: {symbol}")
        return stock
