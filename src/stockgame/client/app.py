"""The Textual application.

Owns the connection, the local state mirror and the screen stack. Every
network call happens in a Textual worker so the UI thread never blocks, and
every inbound frame mutates :class:`ClientState` and then asks the dashboard
to repaint just the view that changed.
"""

from __future__ import annotations

import logging
from typing import Any

from textual import work
from textual.app import App
from textual.binding import Binding

from stockgame.client.config import ClientConfig, normalise_server
from stockgame.client.screens.auth import NewAccountScreen, WelcomeScreen
from stockgame.client.screens.dashboard import DashboardScreen
from stockgame.client.screens.dialogs import (
    ConfirmDialog,
    HelpDialog,
    InsiderDialog,
    PlayerDialog,
    TradeDialog,
)
from stockgame.client.state import ClientState
from stockgame.client.theme import APP_CSS
from stockgame.client.transport import Account, AuthClient, ClientError, GameConnection
from stockgame.shared.money import fmt_money
from stockgame.shared.protocol import ClientMessage, ServerMessage

log = logging.getLogger("stockgame.app")

#: Channels the client always wants.
BASE_CHANNELS = ("market", "portfolio", "news", "tape", "status", "leaderboard")


class StockGameApp(App):
    """Terminal client for the Stock Market Game."""

    CSS = APP_CSS
    TITLE = "Stock Market Game"
    SUB_TITLE = "virtual currency only"

    BINDINGS = [
        Binding("ctrl+c", "quit_game", "Quit", show=False, priority=True),
    ]

    def __init__(
        self,
        config: ClientConfig | None = None,
        *,
        server: str | None = None,
        auto_login: bool = True,
    ) -> None:
        super().__init__()
        self.config = config or ClientConfig.load()
        if server:
            self.config.server = normalise_server(server)
        self.state = ClientState()
        self.connection: GameConnection | None = None
        self.account: Account | None = None
        self.auto_login = auto_login

    # -- lifecycle ----------------------------------------------------------

    def on_mount(self) -> None:
        self.theme = "textual-dark"
        token = self.config.token_for()
        if token and self.auto_login:
            # Resume the previous session without asking for a password again.
            self.push_screen(WelcomeScreen(self.config.server))
            self.resume_session(token)
        else:
            self.push_screen(WelcomeScreen(self.config.server))

    @work(exclusive=True, group="auth")
    async def resume_session(self, token: str) -> None:
        self.account = Account(token=token, username=self.config.last_username)
        try:
            await self._connect(token)
        except ClientError as exc:
            # A stale token is not an error worth alarming anybody about.
            log.debug("stored session rejected: %s", exc)
            self.config.clear_token()
            self.config.save()

    async def on_authenticated(self, account: Account) -> None:
        """Called by the credentials screen once the server accepted a login."""
        self.account = account
        self.config.set_token(account.token)
        self.config.last_username = account.username
        self.config.save()
        self.state.username = account.username
        self.state.is_admin = account.is_admin

        if account.is_new:
            await self.push_screen_wait(
                NewAccountScreen(account.username, account.starting_cash_cents)
            )
        self.start_session(account.token)

    @work(exclusive=True, group="auth")
    async def start_session(self, token: str) -> None:
        try:
            await self._connect(token)
        except ClientError as exc:
            self.notify(exc.message, title="Connection failed", severity="error", timeout=8)

    async def _connect(self, token: str) -> None:
        connection = GameConnection(self.config.server, token)
        self._wire(connection)
        await connection.start()
        self.connection = connection

    def _wire(self, connection: GameConnection) -> None:
        connection.on_status(self._on_connection_status)
        connection.on(ServerMessage.MARKET_SNAPSHOT, self._on_market_snapshot)
        connection.on(ServerMessage.PRICE_UPDATE, self._on_price_update)
        connection.on(ServerMessage.PORTFOLIO, self._on_portfolio)
        connection.on(ServerMessage.STOCK_SNAPSHOT, self._on_stock_snapshot)
        connection.on(ServerMessage.CANDLES, self._on_candles)
        connection.on(ServerMessage.LEADERBOARD, self._on_leaderboard)
        connection.on(ServerMessage.NEWS, self._on_news_batch)
        connection.on(ServerMessage.NEWS_ITEM, self._on_news_item)
        connection.on(ServerMessage.TAPE, self._on_tape)
        connection.on(ServerMessage.TRADE_EXECUTED, self._on_trade_executed)
        connection.on(ServerMessage.ORDER_UPDATE, self._on_order_update)
        connection.on(ServerMessage.ORDERS, self._on_orders)
        connection.on(ServerMessage.TRADES, self._on_trades)
        connection.on(ServerMessage.WATCHLIST, self._on_watchlist)
        connection.on(ServerMessage.PROFILE, self._on_profile)
        connection.on(ServerMessage.MARKET_STATUS, self._on_market_status)
        connection.on(ServerMessage.OK, self._on_ok)

    # -- connection status --------------------------------------------------

    async def _on_connection_status(self, status: str, message: str) -> None:
        self.state.connection_status = status
        self.state.connection_message = message

        if status == "connected" and self.connection is not None:
            welcome = self.connection.welcome
            self.state.username = welcome.get("username", self.state.username)
            self.state.is_admin = bool(welcome.get("is_admin"))
            self.state.market_status = welcome.get("market", {})
            self.state.season = welcome.get("season", {})
            self.state.players_online = welcome.get("players_online", 0)

            if not isinstance(self.screen, DashboardScreen):
                await self.push_screen(DashboardScreen())
            await self.connection.subscribe(*BASE_CHANNELS)
            self.refresh_everything()
            self.notify(f"Connected to {self.config.server}", title="Live", timeout=3)
        elif status == "error":
            self.notify(message, title="Connection", severity="error", timeout=6)

        self._repaint_chrome()

    # -- inbound frames -----------------------------------------------------

    def _on_market_snapshot(self, data: dict[str, Any]) -> None:
        self.state.apply_market_snapshot(data)
        dashboard = self._dashboard()
        if dashboard:
            dashboard.rebuild_tape()
            dashboard.refresh_view("market")

    def _on_price_update(self, data: dict[str, Any]) -> None:
        self.state.apply_price_update(data)
        dashboard = self._dashboard()
        if dashboard is None:
            return
        dashboard.rebuild_tape()
        for view in ("market", "stock", "watchlist", "portfolio"):
            dashboard.refresh_view(view)

    def _on_portfolio(self, data: dict[str, Any]) -> None:
        self.state.portfolio = data
        if data.get("rank"):
            self.state.rank = data["rank"]
        dashboard = self._dashboard()
        if dashboard:
            dashboard.refresh_view("portfolio")
            dashboard.refresh_view("stock")
            dashboard.refresh_view("market")
        self._repaint_chrome()

    def _on_stock_snapshot(self, data: dict[str, Any]) -> None:
        self.state.stock_detail = data
        self._refresh("stock")

    def _on_candles(self, data: dict[str, Any]) -> None:
        if data.get("symbol") != self.state.selected_symbol:
            return  # a late reply for a symbol we have already navigated away from
        self.state.candles = data.get("candles", [])
        self.state.timeframe = data.get("timeframe", self.state.timeframe)
        self._refresh("stock")

    def _on_leaderboard(self, data: dict[str, Any]) -> None:
        self.state.leaderboard = data
        if data.get("season"):
            self.state.season = data["season"]
        if data.get("your_rank"):
            self.state.rank = data["your_rank"]
        self._refresh("leaderboard")
        self._repaint_chrome()

    def _on_news_batch(self, data: dict[str, Any]) -> None:
        self.state.news.clear()
        for item in reversed(data.get("items", [])):
            self.state.news.append(item)
        self._refresh("news")
        self._refresh("market")

    def _on_news_item(self, data: dict[str, Any]) -> None:
        self.state.news.append(data)
        self._refresh("news")
        self._refresh("market")
        self._refresh("stock")
        impact = data.get("impact_pct", 0.0)
        if abs(impact) >= 4:
            # Only surface headlines big enough to matter to a position.
            self.notify(
                data["headline"],
                title=f"{data.get('symbol') or data.get('sector') or 'MARKET'}  {impact:+.1f}%",
                severity="warning" if impact < 0 else "information",
                timeout=7,
            )

    def _on_tape(self, data: dict[str, Any]) -> None:
        self.state.tape.append(data)
        self._refresh("market")

    def _on_trade_executed(self, data: dict[str, Any]) -> None:
        side = data["side"].upper()
        self.notify(
            f"{side} {data['quantity']:,} {data['symbol']} @ {fmt_money(data['price_cents'])}",
            title="Filled",
            timeout=5,
        )
        self.fetch_trades()

    def _on_order_update(self, data: dict[str, Any]) -> None:
        order = data.get("order") or data
        if not isinstance(order, dict) or "id" not in order:
            return
        existing = {row["id"]: index for index, row in enumerate(self.state.orders)}
        if order["id"] in existing:
            self.state.orders[existing[order["id"]]] = order
        else:
            self.state.orders.insert(0, order)
        self._refresh("orders")

    def _on_orders(self, data: dict[str, Any]) -> None:
        self.state.orders = data.get("orders", [])
        self._refresh("orders")

    def _on_trades(self, data: dict[str, Any]) -> None:
        self.state.trades = data.get("trades", [])
        self._refresh("orders")

    def _on_watchlist(self, data: dict[str, Any]) -> None:
        self.state.watchlist = data.get("items", [])
        self._refresh("watchlist")
        self._refresh("market")

    def _on_profile(self, data: dict[str, Any]) -> None:
        self.state.profile = data
        self._refresh("profile")

    def _on_market_status(self, data: dict[str, Any]) -> None:
        self.state.market_status = data
        if data.get("players_online") is not None:
            self.state.players_online = int(data["players_online"])
        self._repaint_chrome()

    def _on_ok(self, data: dict[str, Any]) -> None:
        achievement = data.get("achievement")
        if achievement:
            self.notify(
                f"{achievement['name']} — {achievement['description']}",
                title="★ Achievement unlocked",
                timeout=8,
            )

    # -- outbound actions ---------------------------------------------------

    def open_symbol(self, symbol: str) -> None:
        self.state.selected_symbol = symbol
        self.state.candles = []
        self.state.stock_detail = self.state.stocks.get(symbol, {})
        dashboard = self._dashboard()
        if dashboard:
            dashboard.action_show("stock")
        self.load_symbol(symbol)

    @work(group="insider")
    async def open_insider_desk(self) -> None:
        """Buy a rumour. The desk decides the stock; the fee is never refunded."""
        connection = self.connection
        if connection is None:
            return
        try:
            payload = await connection.request(ClientMessage.GET_TIPS, {})
        except ClientError as exc:
            self.notify(str(exc), severity="warning")
            return

        amount = await self.push_screen_wait(
            InsiderDialog(
                payload.get("tips", []),
                float(payload.get("min_fee", 5000.0)),
                float(payload.get("max_fee", 250000.0)),
            )
        )
        if amount is None:
            return
        try:
            await connection.request(ClientMessage.BUY_TIP, {"amount": amount})
        except ClientError as exc:
            self.notify(str(exc), severity="error")
            return
        self.notify("The desk has taken your money. Watch your tips.", timeout=4)
        self.refresh_everything()

    @work(group="player")
    async def open_player(self, username: str) -> None:
        """Show a rival's book. The server decides what is visible."""
        connection = self.connection
        if connection is None:
            return
        try:
            profile = await connection.request(ClientMessage.GET_PROFILE, {"username": username})
        except ClientError as exc:
            self.notify(str(exc), severity="warning")
            return
        self.push_screen(PlayerDialog(profile))

    @work(exclusive=True, group="symbol")
    async def load_symbol(self, symbol: str) -> None:
        connection = self.connection
        if connection is None:
            return
        try:
            await connection.watch_symbol(symbol)
            payload = await connection.request(
                ClientMessage.GET_CANDLES,
                {"symbol": symbol, "timeframe": self.state.timeframe, "points": 160},
            )
            self._on_candles(payload)
        except ClientError as exc:
            self.notify(exc.message, severity="error")

    def set_timeframe(self, timeframe: str) -> None:
        self.state.timeframe = timeframe
        self.config.default_timeframe = timeframe
        dashboard = self._dashboard()
        if dashboard and dashboard.active_view != "stock":
            dashboard.action_show("stock")
        self.load_symbol(self.state.selected_symbol)

    def toggle_chart_style(self) -> None:
        style = "line" if self.config.chart_style == "candles" else "candles"
        self.config.chart_style = style
        self.state.chart_style = style  # type: ignore[attr-defined]
        self.config.save()
        self._refresh("stock")
        self.notify(f"Chart: {style}", timeout=1.5)

    def open_ticket(self, side: str, symbol: str | None) -> None:
        symbol = symbol or self.state.selected_symbol
        if not symbol or symbol not in self.state.stocks:
            self.notify("Select a symbol first.", severity="warning")
            return
        if side == "sell" and self.state.shares_of(symbol) == 0:
            self.notify(f"You do not hold any {symbol}.", severity="warning")
            return
        self.push_screen(
            TradeDialog(
                symbol,
                side,
                price_cents=self.state.price_of(symbol),
                company_name=self.state.name_of(symbol),
                cash_cents=self.state.cash_cents,
                shares_held=self.state.shares_of(symbol),
            ),
            self._on_ticket_closed,
        )

    def _on_ticket_closed(self, payload: dict[str, Any] | None) -> None:
        if payload:
            self.submit_order(payload)

    @work(group="orders")
    async def submit_order(self, payload: dict[str, Any]) -> None:
        connection = self.connection
        if connection is None:
            self.notify("Not connected.", severity="error")
            return
        try:
            result = await connection.request(ClientMessage.PLACE_ORDER, payload)
        except ClientError as exc:
            self.notify(exc.message, title="Order rejected", severity="error", timeout=8)
            return
        order = result.get("order", {})
        if order.get("status") == "rejected":
            self.notify(
                order.get("reject_reason") or "Order rejected.",
                title="Order rejected",
                severity="error",
                timeout=8,
            )
        elif order.get("status") == "pending":
            self.notify(
                f"Limit order resting: {order['side'].upper()} {order['quantity']:,} "
                f"{order['symbol']}",
                title="Working",
                timeout=5,
            )
        self.fetch_orders()

    @work(group="orders")
    async def cancel_order(self, order_id: str) -> None:
        connection = self.connection
        if connection is None:
            return
        try:
            await connection.request(ClientMessage.CANCEL_ORDER, {"order_id": order_id})
            self.notify("Order cancelled.", timeout=3)
        except ClientError as exc:
            self.notify(exc.message, severity="error")
        self.fetch_orders()

    @work(group="watchlist")
    async def watchlist_add(self, symbol: str) -> None:
        connection = self.connection
        if connection is None:
            return
        try:
            payload = await connection.request(ClientMessage.WATCHLIST_ADD, {"symbol": symbol})
            self._on_watchlist(payload)
            self.notify(f"{symbol} added to watchlist", timeout=2)
        except ClientError as exc:
            self.notify(exc.message, severity="error")

    @work(group="watchlist")
    async def watchlist_remove(self, symbol: str) -> None:
        connection = self.connection
        if connection is None:
            return
        try:
            payload = await connection.request(ClientMessage.WATCHLIST_REMOVE, {"symbol": symbol})
            self._on_watchlist(payload)
            self.notify(f"{symbol} removed from watchlist", timeout=2)
        except ClientError as exc:
            self.notify(exc.message, severity="error")

    @work(group="fetch")
    async def fetch_orders(self) -> None:
        await self._fetch(ClientMessage.GET_ORDERS, {}, self._on_orders)
        await self._fetch(ClientMessage.GET_TRADES, {"limit": 60}, self._on_trades)

    @work(group="fetch")
    async def fetch_trades(self) -> None:
        await self._fetch(ClientMessage.GET_TRADES, {"limit": 60}, self._on_trades)

    @work(group="fetch")
    async def fetch_profile(self) -> None:
        await self._fetch(ClientMessage.GET_PROFILE, {}, self._on_profile)

    @work(group="fetch")
    async def fetch_watchlist(self) -> None:
        await self._fetch(ClientMessage.GET_WATCHLIST, {}, self._on_watchlist)

    async def _fetch(self, kind, payload, handler) -> None:
        connection = self.connection
        if connection is None:
            return
        try:
            handler(await connection.request(kind, payload))
        except ClientError as exc:
            log.debug("fetch %s failed: %s", kind, exc)

    def refresh_everything(self) -> None:
        self.fetch_orders()
        self.fetch_watchlist()
        self.fetch_profile()
        self.load_symbol(self.state.selected_symbol)

    def on_view_changed(self, view_id: str) -> None:
        """Lazily fetch data the newly-opened view needs."""
        if view_id == "profile":
            self.fetch_profile()
        elif view_id == "orders":
            self.fetch_orders()
        elif view_id == "watchlist":
            self.fetch_watchlist()

    # -- global actions -----------------------------------------------------

    def action_help(self) -> None:
        self.push_screen(HelpDialog())

    def action_quit_game(self) -> None:
        self.push_screen(
            ConfirmDialog(
                "Quit Stock Market Game?",
                "Your portfolio stays exactly as it is. The market keeps running "
                "while you are away.",
            ),
            self._on_quit_confirmed,
        )

    def _on_quit_confirmed(self, confirmed: bool | None) -> None:
        if confirmed:
            self.exit()

    def set_server(self, server: str) -> None:
        self.config.server = normalise_server(server)
        self.config.save()
        self.notify(f"Server set to {self.config.server}", timeout=3)

    async def on_unmount(self) -> None:
        if self.connection is not None:
            await self.connection.close()

    # -- helpers ------------------------------------------------------------

    def _dashboard(self) -> DashboardScreen | None:
        for screen in self.screen_stack:
            if isinstance(screen, DashboardScreen):
                return screen
        return None

    def _refresh(self, view_id: str) -> None:
        dashboard = self._dashboard()
        if dashboard:
            dashboard.refresh_view(view_id)

    def _repaint_chrome(self) -> None:
        dashboard = self._dashboard()
        if dashboard:
            dashboard._refresh_chrome()


async def logout(config: ClientConfig) -> None:
    """Invalidate the stored session server-side, then forget it locally."""
    token = config.token_for()
    if token:
        await AuthClient(config.server).logout(token)
    config.clear_token()
    config.save()
