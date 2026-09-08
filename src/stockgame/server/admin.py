"""``stockgame-admin`` -- operate on the database directly.

Runs against the database rather than the HTTP API, so it works whether or
not the server is up, and there is no network-exposed admin surface to
attack. Access control is filesystem access to the database itself.

Note that price-affecting commands (``halt``) write a flag the running server
picks up on its next flush; the running process remains authoritative for
live prices.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import datetime, timezone

from sqlalchemy import delete, func, select

from stockgame import __version__
from stockgame.server.config import get_settings
from stockgame.server.db.models import (
    Holding,
    LeaderboardEntry,
    MarketEvent,
    MarketState,
    Order,
    Portfolio,
    Season,
    Session,
    Stock,
    Trade,
    User,
)
from stockgame.server.db.session import Database
from stockgame.shared.money import fmt_money, fmt_pct


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="stockgame-admin", description="Inspect and administer a Stock Market Game server."
    )
    parser.add_argument("--database-url", help="Override STOCKGAME_DATABASE_URL")
    parser.add_argument("--version", action="version", version=f"stockgame {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("status", help="Server, market and season overview")
    sub.add_parser("players", help="List every account with its portfolio value")
    sub.add_parser("market", help="List every listed stock and its price")

    player = sub.add_parser("player", help="Detail for one account")
    player.add_argument("username")

    trades = sub.add_parser("trades", help="Recent trades across all players")
    trades.add_argument("--limit", type=int, default=30)
    trades.add_argument("--user", help="Filter to one username")

    news = sub.add_parser("news", help="Recent market events")
    news.add_argument("--limit", type=int, default=20)

    promote = sub.add_parser("promote", help="Grant admin rights")
    promote.add_argument("username")

    demote = sub.add_parser("demote", help="Revoke admin rights")
    demote.add_argument("username")

    suspend = sub.add_parser("suspend", help="Disable an account and end its sessions")
    suspend.add_argument("username")

    reinstate = sub.add_parser("reinstate", help="Re-enable a suspended account")
    reinstate.add_argument("username")

    sessions = sub.add_parser("sessions", help="Session maintenance")
    sessions.add_argument("--purge", action="store_true", help="Delete expired sessions")
    sessions.add_argument("--revoke-user", help="End every session for a username")

    halt = sub.add_parser("halt", help="Halt trading in one symbol")
    halt.add_argument("symbol")
    halt.add_argument("--resume", action="store_true", help="Resume instead of halting")

    season = sub.add_parser("season", help="Season management")
    season.add_argument("--end", action="store_true", help="End the current season now")

    return parser


async def run(args: argparse.Namespace) -> int:
    settings = get_settings()
    if args.database_url:
        settings.database_url = args.database_url
    db = Database(settings)
    try:
        handler = _COMMANDS[args.command]
        return await handler(db, settings, args)
    finally:
        await db.dispose()


# -- commands ---------------------------------------------------------------


async def cmd_status(db: Database, settings, args) -> int:
    async with db.session() as session:
        state = await session.get(MarketState, 1)
        users = await session.scalar(select(func.count()).select_from(User))
        trades = await session.scalar(select(func.count()).select_from(Trade))
        orders = await session.scalar(select(func.count()).select_from(Order))
        open_orders = await session.scalar(
            select(func.count())
            .select_from(Order)
            .where(Order.status.in_(("pending", "partially_filled")))
        )
        stocks = await session.scalar(select(func.count()).select_from(Stock))
        season = await session.scalar(
            select(Season).where(Season.is_active.is_(True)).order_by(Season.number.desc())
        )
        cash = await session.scalar(select(func.sum(Portfolio.cash_cents))) or 0

    print(f"\n  STOCK MARKET GAME · admin v{__version__}")
    print(f"  database   {settings.database_url}")
    if state is None:
        print("\n  Market has not been seeded yet. Start the server once.\n")
        return 0
    print(f"\n  market     {state.status}   regime: {state.regime}")
    print(f"  day        {state.day_index}   tick: {state.tick_count}")
    print(f"  listed     {stocks} symbols")
    if season:
        print(f"  season     {season.number} · {season.name}")
    print(f"\n  players    {users}")
    print(f"  orders     {orders} ({open_orders} open)")
    print(f"  trades     {trades}")
    print(f"  total cash {fmt_money(int(cash))}\n")
    return 0


async def cmd_players(db: Database, settings, args) -> int:
    async with db.session() as session:
        rows = (
            await session.execute(
                select(User, Portfolio)
                .join(Portfolio, Portfolio.user_id == User.id)
                .order_by(Portfolio.last_value_cents.desc())
            )
        ).all()
    if not rows:
        print("\n  No accounts yet.\n")
        return 0
    print(f"\n  {'USERNAME':<20} {'VALUE':>14} {'CASH':>14} {'RETURN':>9}  {'TRADES':>6}  FLAGS")
    print("  " + "─" * 76)
    for user, portfolio in rows:
        value = portfolio.last_value_cents or portfolio.cash_cents
        pct = (
            (value - portfolio.deposited_cents) / portfolio.deposited_cents * 100
            if portfolio.deposited_cents
            else 0.0
        )
        flags = " ".join(
            filter(None, ["admin" if user.is_admin else "", "" if user.is_active else "SUSPENDED"])
        )
        print(
            f"  {user.username:<20} {fmt_money(value):>14} {fmt_money(portfolio.cash_cents):>14} "
            f"{fmt_pct(pct):>9}  {portfolio.total_trades:>6}  {flags}"
        )
    print()
    return 0


async def cmd_market(db: Database, settings, args) -> int:
    async with db.session() as session:
        stocks = (await session.execute(select(Stock).order_by(Stock.symbol))).scalars().all()
    print(f"\n  {'SYM':<7}{'NAME':<26}{'SECTOR':<16}{'PRICE':>11}{'CHANGE':>9}  STATE")
    print("  " + "─" * 76)
    for stock in stocks:
        prev = stock.prev_close_cents or stock.price_cents
        pct = (stock.price_cents - prev) / prev * 100 if prev else 0.0
        state = "HALTED" if stock.is_halted else ""
        print(
            f"  {stock.symbol:<7}{stock.name[:25]:<26}{stock.sector:<16}"
            f"{fmt_money(stock.price_cents):>11}{fmt_pct(pct):>9}  {state}"
        )
    print()
    return 0


async def cmd_player(db: Database, settings, args) -> int:
    async with db.session() as session:
        user = await session.scalar(
            select(User).where(User.username_lower == args.username.lower())
        )
        if user is None:
            print(f"\n  No account named {args.username!r}.\n", file=sys.stderr)
            return 1
        portfolio = await session.scalar(select(Portfolio).where(Portfolio.user_id == user.id))
        prices = dict((await session.execute(select(Stock.symbol, Stock.price_cents))).all())
        positions = (
            await session.execute(
                select(Holding, Stock.symbol)
                .join(Stock, Stock.id == Holding.stock_id)
                .where(Holding.portfolio_id == portfolio.id)
            )
        ).all()
        sessions_open = await session.scalar(
            select(func.count())
            .select_from(Session)
            .where(
                Session.user_id == user.id,
                Session.expires_at > datetime.now(timezone.utc),
            )
        )

    holdings_value = sum(prices.get(symbol, 0) * h.quantity for h, symbol in positions)
    total = portfolio.cash_cents + holdings_value
    print(
        f"\n  {user.username}{'  [admin]' if user.is_admin else ''}"
        f"{'  [SUSPENDED]' if not user.is_active else ''}"
    )
    print(f"  joined      {user.created_at:%Y-%m-%d %H:%M} UTC")
    print(
        f"  last login  {user.last_login_at:%Y-%m-%d %H:%M} UTC"
        if user.last_login_at
        else "  last login  never"
    )
    print(f"  sessions    {sessions_open} active")
    print(f"\n  cash        {fmt_money(portfolio.cash_cents):>16}")
    print(f"  holdings    {fmt_money(holdings_value):>16}")
    print(f"  total       {fmt_money(total):>16}")
    print(f"  deposited   {fmt_money(portfolio.deposited_cents):>16}")
    print(f"  realised    {fmt_money(portfolio.realized_pl_cents, sign=True):>16}")
    print(
        f"  trades      {portfolio.total_trades} "
        f"({portfolio.winning_trades}W / {portfolio.losing_trades}L)"
    )
    if positions:
        print(f"\n  {'SYMBOL':<8}{'QTY':>10}{'AVG':>12}{'PRICE':>12}{'VALUE':>14}")
        print("  " + "─" * 56)
        for holding, symbol in positions:
            price = prices.get(symbol, 0)
            print(
                f"  {symbol:<8}{holding.quantity:>10,}"
                f"{fmt_money(holding.avg_cost_cents):>12}{fmt_money(price):>12}"
                f"{fmt_money(price * holding.quantity):>14}"
            )
    print()
    return 0


async def cmd_trades(db: Database, settings, args) -> int:
    async with db.session() as session:
        query = (
            select(Trade, Stock.symbol, User.username)
            .join(Stock, Stock.id == Trade.stock_id)
            .join(User, User.id == Trade.user_id)
            .order_by(Trade.created_at.desc())
            .limit(args.limit)
        )
        if args.user:
            query = query.where(User.username_lower == args.user.lower())
        rows = (await session.execute(query)).all()
    if not rows:
        print("\n  No trades yet.\n")
        return 0
    print(f"\n  {'WHEN':<20}{'PLAYER':<16}{'SIDE':<6}{'SYM':<7}{'QTY':>9}{'PRICE':>12}{'P/L':>12}")
    print("  " + "─" * 82)
    for trade, symbol, username in rows:
        print(
            f"  {trade.created_at:%Y-%m-%d %H:%M:%S}  {username:<16}{trade.side.upper():<6}"
            f"{symbol:<7}{trade.quantity:>9,}{fmt_money(trade.price_cents):>12}"
            f"{fmt_money(trade.realized_pl_cents, sign=True) if trade.realized_pl_cents else '':>12}"
        )
    print()
    return 0


async def cmd_news(db: Database, settings, args) -> int:
    async with db.session() as session:
        rows = (
            (
                await session.execute(
                    select(MarketEvent).order_by(MarketEvent.created_at.desc()).limit(args.limit)
                )
            )
            .scalars()
            .all()
        )
    print()
    for event in rows:
        tag = event.symbol or event.sector or "MARKET"
        print(
            f"  {event.created_at:%Y-%m-%d %H:%M}  [{tag:^10}] "
            f"{event.impact * 100:+.1f}%  {event.headline}"
        )
    print()
    return 0


async def _set_flag(db: Database, username: str, **values) -> int:
    async with db.write_session() as session:
        user = await session.scalar(select(User).where(User.username_lower == username.lower()))
        if user is None:
            print(f"\n  No account named {username!r}.\n", file=sys.stderr)
            return 1
        for key, value in values.items():
            setattr(user, key, value)
        if values.get("is_active") is False:
            await session.execute(delete(Session).where(Session.user_id == user.id))
        print(f"\n  {user.username}: {', '.join(f'{k}={v}' for k, v in values.items())}\n")
    return 0


async def cmd_promote(db, settings, args) -> int:
    return await _set_flag(db, args.username, is_admin=True)


async def cmd_demote(db, settings, args) -> int:
    return await _set_flag(db, args.username, is_admin=False)


async def cmd_suspend(db, settings, args) -> int:
    return await _set_flag(db, args.username, is_active=False)


async def cmd_reinstate(db, settings, args) -> int:
    return await _set_flag(db, args.username, is_active=True)


async def cmd_sessions(db: Database, settings, args) -> int:
    if args.revoke_user:
        async with db.write_session() as session:
            user = await session.scalar(
                select(User).where(User.username_lower == args.revoke_user.lower())
            )
            if user is None:
                print(f"\n  No account named {args.revoke_user!r}.\n", file=sys.stderr)
                return 1
            result = await session.execute(delete(Session).where(Session.user_id == user.id))
            print(f"\n  Revoked {result.rowcount or 0} session(s) for {user.username}.\n")
        return 0
    if args.purge:
        async with db.write_session() as session:
            result = await session.execute(
                delete(Session).where(Session.expires_at <= datetime.now(timezone.utc))
            )
            print(f"\n  Purged {result.rowcount or 0} expired session(s).\n")
        return 0
    async with db.session() as session:
        rows = (
            await session.execute(
                select(User.username, func.count(Session.id))
                .join(Session, Session.user_id == User.id)
                .where(Session.expires_at > datetime.now(timezone.utc))
                .group_by(User.username)
            )
        ).all()
    print("\n  ACTIVE SESSIONS")
    for username, count in rows or []:
        print(f"    {username:<20} {count}")
    print()
    return 0


async def cmd_halt(db: Database, settings, args) -> int:
    symbol = args.symbol.upper()
    async with db.write_session() as session:
        stock = await session.scalar(select(Stock).where(Stock.symbol == symbol))
        if stock is None:
            print(f"\n  No symbol named {symbol!r}.\n", file=sys.stderr)
            return 1
        stock.is_halted = not args.resume
        state = "resumed" if args.resume else "halted"
        print(f"\n  {symbol} {state}. Restart the server for it to take effect live.\n")
    return 0


async def cmd_season(db: Database, settings, args) -> int:
    async with db.write_session() as session:
        season = await session.scalar(
            select(Season).where(Season.is_active.is_(True)).order_by(Season.number.desc())
        )
        if season is None:
            print("\n  No active season.\n")
            return 0
        if args.end:
            season.is_active = False
            season.ends_at = datetime.now(timezone.utc)
            await session.execute(delete(LeaderboardEntry))
            print(
                f"\n  Ended season {season.number} ({season.name}). "
                "A new one starts on the next leaderboard pass.\n"
            )
        else:
            print(f"\n  Season {season.number}: {season.name}")
            print(f"  started {season.started_at:%Y-%m-%d}")
            print(f"  ends    {season.ends_at:%Y-%m-%d}\n" if season.ends_at else "")
    return 0


_COMMANDS = {
    "status": cmd_status,
    "players": cmd_players,
    "market": cmd_market,
    "player": cmd_player,
    "trades": cmd_trades,
    "news": cmd_news,
    "promote": cmd_promote,
    "demote": cmd_demote,
    "suspend": cmd_suspend,
    "reinstate": cmd_reinstate,
    "sessions": cmd_sessions,
    "halt": cmd_halt,
    "season": cmd_season,
}


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:  # pragma: no cover
        return 130


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
