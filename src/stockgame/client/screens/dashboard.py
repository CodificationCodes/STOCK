"""The trading desk -- the screen the player lives in.

Layout::

    ┌ top bar ─ index · regime · simulated day · season · players online ─┐
    ├ nav bar ─ M T P W O L N U ───────────────────────────────────────────┤
    │                                                                      │
    │                        active view (ContentSwitcher)                 │
    │                                                                      │
    ├ status bar ─ cash · value · today · total · rank · connection ───────┤
    └ ticker tape ─ scrolling prices ──────────────────────────────────────┘

Everything is keyboard driven. The screen owns the bindings and delegates to
the app for anything that needs the network.
"""

from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.screen import Screen
from textual.widgets import ContentSwitcher, DataTable

from stockgame.client.screens.views import (
    LeaderboardView,
    MarketView,
    NewsView,
    OrdersView,
    PortfolioView,
    ProfileView,
    StockView,
    WatchlistView,
)
from stockgame.client.widgets.panels import NavBar, StatusBar, TickerTape, TopBar

#: How often the marquee shifts. Fast enough to read as motion, slow enough
#: that it costs nothing.
TAPE_INTERVAL = 0.2
#: The header shows a progress bar for the simulated day, so it needs a tick
#: of its own independent of price updates.
CHROME_INTERVAL = 1.0

VIEW_IDS = (
    "market",
    "stock",
    "portfolio",
    "watchlist",
    "orders",
    "leaderboard",
    "news",
    "profile",
)


class DashboardScreen(Screen):
    BINDINGS = [
        Binding("m", "show('market')", "Market"),
        Binding("t", "show('stock')", "Ticker"),
        Binding("p", "show('portfolio')", "Portfolio"),
        Binding("w", "show('watchlist')", "Watchlist"),
        Binding("o", "show('orders')", "Orders"),
        Binding("l", "show('leaderboard')", "League"),
        Binding("n", "show('news')", "News"),
        Binding("u", "show('profile')", "Profile"),
        Binding("question_mark", "help", "Help"),
        Binding("b", "buy", "Buy"),
        Binding("s", "sell", "Sell"),
        Binding("c", "cancel_order", "Cancel", show=False),
        Binding("f", "cycle_sort", "Sort", show=False),
        Binding("i", "insider", "Insider desk", show=False),
        Binding("g", "cycle_board", "Board", show=False),
        Binding("v", "toggle_chart", "Chart", show=False),
        Binding("r", "refresh_all", "Refresh", show=False),
        Binding("plus,equals_sign", "watch_add", "Watch", show=False),
        Binding("minus", "watch_remove", "Unwatch", show=False),
        Binding("1", "timeframe('1D')", "1D", show=False),
        Binding("2", "timeframe('1W')", "1W", show=False),
        Binding("3", "timeframe('1M')", "1M", show=False),
        Binding("4", "timeframe('3M')", "3M", show=False),
        Binding("5", "timeframe('1Y')", "1Y", show=False),
        Binding("enter", "open_selected", "Open", show=False, priority=True),
        Binding("escape", "back", "Back", show=False),
        Binding("q", "quit_game", "Quit"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.active_view = "market"
        self._previous_view = "market"
        #: Views that have pending data but are not on screen.
        self._dirty: set[str] = set()

    # -- composition --------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield TopBar(id="topbar")
        yield NavBar(id="navbar")
        with ContentSwitcher(initial="market", id="content"):
            yield MarketView(id="market")
            yield StockView(id="stock")
            yield PortfolioView(id="portfolio")
            yield WatchlistView(id="watchlist")
            yield OrdersView(id="orders")
            yield LeaderboardView(id="leaderboard")
            yield NewsView(id="news")
            yield ProfileView(id="profile")
        yield StatusBar(id="statusbar")
        yield TickerTape(id="tape")

    def on_mount(self) -> None:
        self.query_one("#navbar", NavBar).render_state(self.active_view)
        self.set_interval(TAPE_INTERVAL, self._advance_tape)
        self.set_interval(CHROME_INTERVAL, self._refresh_chrome)
        self.refresh_all_views()

    # -- refresh ------------------------------------------------------------

    @property
    def state(self):
        return self.app.state

    def refresh_all_views(self) -> None:
        self._refresh_chrome()
        for view_id in VIEW_IDS:
            self.refresh_view(view_id)

    def refresh_view(self, view_id: str) -> None:
        """Re-render one view, but only if it is the one on screen.

        Off-screen views are marked dirty and redrawn when switched to, so a
        price tick repaints one table rather than eight.
        """
        if view_id != self.active_view:
            self._dirty.add(view_id)
            return
        try:
            widget = self.query_one(f"#{view_id}")
        except Exception:
            return
        refresh = getattr(widget, "refresh_view", None)
        if refresh is not None:
            refresh(self.state)
        self._dirty.discard(view_id)

    def _refresh_chrome(self) -> None:
        state = self.state
        self.query_one("#topbar", TopBar).render_state(state)
        self.query_one("#statusbar", StatusBar).render_state(state)
        # Keep the simulated-day progress bar moving between server pushes.
        status = state.market_status
        if status.get("day_seconds"):
            status["day_elapsed"] = min(
                status["day_seconds"], status.get("day_elapsed", 0) + CHROME_INTERVAL
            )

    def _advance_tape(self) -> None:
        # Skip entirely when a modal is up or the tape is disabled: an
        # animation nobody can see is pure wasted repaint.
        if not self.app.config.show_tape or self.app.screen is not self:
            return
        self.query_one("#tape", TickerTape).advance()

    def rebuild_tape(self) -> None:
        self.query_one("#tape", TickerTape).rebuild(self.state)

    # -- navigation ---------------------------------------------------------

    def action_show(self, view_id: str) -> None:
        if view_id == self.active_view:
            return
        self._previous_view = self.active_view
        self.active_view = view_id
        self.query_one("#content", ContentSwitcher).current = view_id
        self.query_one("#navbar", NavBar).render_state(view_id)
        self.refresh_view(view_id)
        self.app.on_view_changed(view_id)
        self._focus_table(view_id)

    def _focus_table(self, view_id: str) -> None:
        try:
            table = self.query_one(f"#{view_id}").query(DataTable).first()
        except Exception:
            return
        if table is not None:
            table.focus()

    def action_back(self) -> None:
        if self.active_view == "stock":
            self.action_show(self._previous_view if self._previous_view != "stock" else "market")

    def action_open_selected(self) -> None:
        if self.active_view == "leaderboard":
            username = self.selected_player()
            if username:
                self.app.open_player(username)
            return
        symbol = self.selected_symbol()
        if symbol:
            self.app.open_symbol(symbol)

    def selected_player(self) -> str | None:
        """The leaderboard row under the cursor. Keys are ``period:username``."""
        try:
            from stockgame.client.screens.views import KeyedTable

            key = self.query_one("#leaderboard-table", KeyedTable).selected_key()
        except Exception:
            return None
        if not key or ":" not in key:
            return None
        return key.split(":", 1)[1]

    def selected_symbol(self) -> str | None:
        """Whatever symbol the current view considers selected."""
        if self.active_view == "stock":
            return self.state.selected_symbol
        table_ids = {
            "market": "#market-table",
            "portfolio": "#portfolio-table",
            "watchlist": "#watchlist-table",
        }
        table_id = table_ids.get(self.active_view)
        if table_id is None:
            return self.state.selected_symbol
        try:
            from stockgame.client.screens.views import KeyedTable

            return self.query_one(table_id, KeyedTable).selected_key()
        except Exception:
            return self.state.selected_symbol

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        """Track the cursor so B/S act on what the player is looking at."""
        if self.active_view in ("market", "portfolio", "watchlist"):
            key = event.row_key.value
            if key and key in self.state.stocks:
                self.state.selected_symbol = key

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        key = event.row_key.value
        if key and key in self.state.stocks:
            self.app.open_symbol(key)

    # -- actions delegated to the app ---------------------------------------

    def action_buy(self) -> None:
        self.app.open_ticket("buy", self.selected_symbol())

    def action_sell(self) -> None:
        self.app.open_ticket("sell", self.selected_symbol())

    def action_cancel_order(self) -> None:
        if self.active_view != "orders":
            self.app.notify("Open the Orders screen (O) to cancel an order.", severity="warning")
            return
        from stockgame.client.screens.views import KeyedTable

        order_id = self.query_one("#orders-table", KeyedTable).selected_key()
        if order_id:
            self.app.cancel_order(order_id)

    def action_timeframe(self, timeframe: str) -> None:
        self.app.set_timeframe(timeframe)

    def action_toggle_chart(self) -> None:
        self.app.toggle_chart_style()

    def action_watch_add(self) -> None:
        symbol = self.selected_symbol()
        if symbol:
            self.app.watchlist_add(symbol)

    def action_watch_remove(self) -> None:
        symbol = self.selected_symbol()
        if symbol:
            self.app.watchlist_remove(symbol)

    def action_insider(self) -> None:
        self.app.open_insider_desk()

    def action_cycle_sort(self) -> None:
        if self.active_view == "market":
            label = self.query_one("#market", MarketView).cycle_sort()
            self.refresh_view("market")
            self.app.notify(f"Sorted by {label}", timeout=1.5)

    def action_cycle_board(self) -> None:
        if self.active_view == "leaderboard":
            label = self.query_one("#leaderboard", LeaderboardView).cycle_period()
            self.refresh_view("leaderboard")
            self.app.notify(f"Showing {label}", timeout=1.5)

    def action_refresh_all(self) -> None:
        self.app.refresh_everything()

    def action_help(self) -> None:
        self.app.action_help()

    def action_quit_game(self) -> None:
        self.app.action_quit_game()
