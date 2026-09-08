"""The dashboard views.

Each view owns one screen of the terminal and exposes ``refresh_view(state)``.
Tables are updated **cell by cell** rather than cleared and rebuilt: with a
two-second tick, rebuilding would reset the cursor and make the list unusable
to navigate, and would repaint rows that did not change.
"""

from __future__ import annotations

import contextlib
from typing import Any

from rich.console import Group
from rich.table import Table
from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Container, Horizontal, VerticalScroll
from textual.widgets import DataTable, Static

from stockgame.client.theme import (
    ACCENT,
    CHART_GRID,
    DOWN,
    INFO,
    NEUTRAL,
    TEXT,
    TEXT_DIM,
    UP,
    sector_colour,
)
from stockgame.client.widgets.chart import (
    Bar,
    PriceChart,
    day_range_marker,
    format_change,
    heat_style,
    scale_bar,
)
from stockgame.client.widgets.panels import NewsPanel, SectorPanel, TradeTapePanel
from stockgame.shared.money import fmt_money, fmt_pct

TIMEFRAMES = ("1D", "1W", "1M", "3M", "1Y")


class KeyedTable(DataTable):
    """A DataTable that updates in place instead of being rebuilt."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(cursor_type="row", zebra_stripes=False, **kwargs)
        self._column_keys: list[str] = []
        self._row_keys: set[str] = set()
        #: Last rendered cells per row, so unchanged cells are never re-sent.
        self._cache: dict[str, list[Any]] = {}

    def setup(self, columns: list[tuple[str, str, int]]) -> None:
        """``columns`` is a list of ``(key, label, width)``."""
        self._column_keys = [key for key, _, _ in columns]
        for key, label, width in columns:
            self.add_column(Text(label, style=NEUTRAL), key=key, width=width)

    def sync(self, rows: list[tuple[str, list[Any]]]) -> None:
        """Add, update and remove rows so the table matches ``rows`` exactly."""
        wanted = {key for key, _ in rows}
        for stale in self._row_keys - wanted:
            with contextlib.suppress(Exception):  # the row may already be gone
                self.remove_row(stale)
            self._cache.pop(stale, None)
        self._row_keys &= wanted

        for key, cells in rows:
            if key in self._row_keys:
                # Most cells (symbol, company, sector) never change. Diffing
                # here turns a 47x7 table refresh into a handful of updates,
                # which is the difference between a smooth tick and a stutter.
                previous = self._cache.get(key)
                for index, (column_key, value) in enumerate(
                    zip(self._column_keys, cells, strict=False)
                ):
                    if previous is not None and index < len(previous) and previous[index] == value:
                        continue
                    self.update_cell(key, column_key, value, update_width=False)
            else:
                self.add_row(*cells, key=key)
                self._row_keys.add(key)
            self._cache[key] = list(cells)

    def selected_key(self) -> str | None:
        if self.cursor_row < 0 or not self.row_count:
            return None
        try:
            return str(self.coordinate_to_cell_key((self.cursor_row, 0)).row_key.value)
        except Exception:  # pragma: no cover - transient during rebuilds
            return None


# ---------------------------------------------------------------------------
# Market
# ---------------------------------------------------------------------------


class MarketView(Container):
    """The full board, plus sector heat, the live tape and headlines."""

    SORTS = (
        ("symbol", "SYMBOL", False),
        ("change_pct", "GAINERS", True),
        ("change_pct", "LOSERS", False),
        ("volume", "VOLUME", True),
        ("price_cents", "PRICE", True),
    )

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.sort_index = 0

    def compose(self) -> ComposeResult:
        with Horizontal():
            with Container(id="market-left", classes="panel column"):
                yield Static("", id="market-heading", classes="panel-title")
                yield KeyedTable(id="market-table")
            with VerticalScroll(id="market-right", classes="column"):
                yield Static(Text(" SECTORS", style=f"bold {ACCENT}"))
                yield SectorPanel(id="market-sectors", classes="panel")
                yield Static(Text(" LIVE TAPE", style=f"bold {ACCENT}"))
                yield TradeTapePanel(id="market-tape", classes="panel")
                yield Static(Text(" HEADLINES", style=f"bold {ACCENT}"))
                yield NewsPanel(limit=4, id="market-news", classes="panel")

    def on_mount(self) -> None:
        self.query_one("#market-table", KeyedTable).setup(
            [
                ("symbol", "SYM", 7),
                ("name", "COMPANY", 20),
                ("sector", "SECTOR", 13),
                ("price", "PRICE", 11),
                ("change", "CHANGE", 9),
                ("range", "DAY RANGE", 14),
                ("volume", "VOLUME", 11),
            ]
        )

    def cycle_sort(self) -> str:
        self.sort_index = (self.sort_index + 1) % len(self.SORTS)
        return self.SORTS[self.sort_index][1]

    def refresh_view(self, state) -> None:
        key, label, reverse = self.SORTS[self.sort_index]
        rows = sorted(state.stocks.values(), key=lambda row: row.get(key, 0), reverse=reverse)
        held = {position["symbol"] for position in state.portfolio.get("positions", [])}
        watched = state.watchlist_symbols()

        self.query_one("#market-heading", Static).update(
            Text.assemble(
                (" MARKET  ", f"bold {ACCENT}"),
                (f"{len(rows)} symbols", TEXT_DIM),
                ("   sorted by ", TEXT_DIM),
                (label, INFO),
                ("   F to re-sort", TEXT_DIM),
            )
        )

        payload = []
        for row in rows:
            symbol = row["symbol"]
            marker = "◆ " if symbol in held else ("· " if symbol in watched else "  ")
            payload.append(
                (
                    symbol,
                    [
                        Text(f"{marker}{symbol}", style="bold white"),
                        Text(row["name"][:19], style=TEXT),
                        Text(row["sector"], style=sector_colour(row["sector"])),
                        Text(fmt_money(row["price_cents"]), style="bold white"),
                        Text(
                            f"{row.get('change_pct', 0.0):+.2f}%",
                            style=heat_style(row.get("change_pct", 0.0)),
                        ),
                        day_range_marker(
                            row.get("day_low_cents", row["price_cents"]),
                            row.get("day_high_cents", row["price_cents"]),
                            row["price_cents"],
                            10,
                        ),
                        Text(f"{row.get('volume', 0):,}", style=TEXT_DIM),
                    ],
                )
            )
        self.query_one("#market-table", KeyedTable).sync(payload)
        self.query_one("#market-sectors", SectorPanel).render_state(state)
        self.query_one("#market-tape", TradeTapePanel).render_state(state)
        self.query_one("#market-news", NewsPanel).render_state(state)


# ---------------------------------------------------------------------------
# Stock detail
# ---------------------------------------------------------------------------


class StockView(Container):
    """One company: quote, chart, statistics, your position, its news."""

    def compose(self) -> ComposeResult:
        with Container(classes="panel column"):
            yield Static("", id="stock-header")
            yield Static("", id="stock-chart")
            yield Static("", id="stock-timeframes")
            with Horizontal(id="stock-lower"):
                yield Static("", id="stock-stats")
                yield Static("", id="stock-position")
            yield Static("", id="stock-news")

    def refresh_view(self, state) -> None:
        detail = state.stock_detail or state.stocks.get(state.selected_symbol) or {}
        if not detail:
            self.query_one("#stock-header", Static).update(
                Text("\n  Select a symbol on the market screen.", style=TEXT_DIM)
            )
            return

        self.query_one("#stock-header", Static).update(self._header(detail))
        self.query_one("#stock-chart", Static).update(self._chart(state, detail))
        self.query_one("#stock-timeframes", Static).update(self._timeframes(state))
        self.query_one("#stock-stats", Static).update(self._stats(detail))
        self.query_one("#stock-position", Static).update(self._position(state, detail))
        self.query_one("#stock-news", Static).update(self._news(state, detail))

    def _header(self, detail: dict[str, Any]) -> Text:
        change_pct = detail.get("change_pct", 0.0)
        style = UP if change_pct >= 0 else DOWN
        line = Text()
        line.append(f"\n  {detail.get('symbol', '')}", style=f"bold {ACCENT}")
        line.append(f"  {detail.get('name', '')}", style="bold white")
        line.append(
            f"   {detail.get('sector', '')}\n", style=sector_colour(detail.get("sector", ""))
        )
        line.append(f"  {fmt_money(detail.get('price_cents', 0))}", style=f"bold {style}")
        line.append("   ")
        line.append(format_change(change_pct))
        line.append(f"  ({fmt_money(detail.get('change_cents', 0), sign=True)})", style=style)
        if detail.get("is_halted"):
            line.append("   HALTED", style=f"bold {DOWN}")
        line.append("\n")
        return line

    def _chart(self, state, detail: dict[str, Any]) -> Any:
        bars = [Bar.from_payload(row) for row in state.candles]
        return PriceChart(
            bars,
            style=getattr(state, "chart_style", "candles"),
            reference=detail.get("prev_close_cents"),
            show_volume=True,
        )

    def _timeframes(self, state) -> Text:
        line = Text("  ")
        for index, frame in enumerate(TIMEFRAMES, start=1):
            active = frame == state.timeframe
            line.append(f" {index} ", style=TEXT_DIM)
            line.append(frame, style=f"bold {ACCENT} reverse" if active else NEUTRAL)
            line.append("  ")
        line.append("   V toggle candles/line   B buy   S sell   +/- watchlist", style=TEXT_DIM)
        return line

    def _stats(self, detail: dict[str, Any]) -> Group:
        table = Table.grid(padding=(0, 2))
        table.add_column(width=14)
        table.add_column(width=14, justify="right")
        rows = (
            ("Open", fmt_money(detail.get("open_cents", 0))),
            ("Prev close", fmt_money(detail.get("prev_close_cents", 0))),
            ("Day high", fmt_money(detail.get("day_high_cents", 0))),
            ("Day low", fmt_money(detail.get("day_low_cents", 0))),
            ("Volume", f"{detail.get('volume', 0):,}"),
            ("Market cap", fmt_money(detail.get("market_cap_cents", 0), compact=True)),
            ("Volatility", f"{detail.get('volatility', 0) * 100:.0f}%"),
            ("Beta", f"{detail.get('beta', 0):.2f}"),
        )
        for label, value in rows:
            table.add_row(Text(label, style=TEXT_DIM), Text(value, style=TEXT))
        return Group(Text("  STATISTICS", style=f"bold {ACCENT}"), table)

    def _position(self, state, detail: dict[str, Any]) -> Group:
        position = state.position_in(detail.get("symbol", ""))
        table = Table.grid(padding=(0, 2))
        table.add_column(width=14)
        table.add_column(width=16, justify="right")
        if position:
            pl = position["unrealized_pl_cents"]
            style = UP if pl >= 0 else DOWN
            rows = (
                ("Shares", Text(f"{position['quantity']:,}", style="bold white")),
                ("Avg cost", Text(fmt_money(position["avg_cost_cents"]), style=TEXT)),
                ("Market value", Text(fmt_money(position["market_value_cents"]), style=TEXT)),
                ("Unrealised P/L", Text(fmt_money(pl, sign=True), style=style)),
                ("Return", Text(fmt_pct(position["return_pct"]), style=style)),
                ("Weight", Text(f"{position['weight_pct']:.1f}%", style=TEXT_DIM)),
            )
        else:
            rows = (
                ("Shares", Text("0", style=TEXT_DIM)),
                ("Buying power", Text(fmt_money(state.cash_cents), style=TEXT)),
                (
                    "Affordable",
                    Text(
                        f"{state.cash_cents // max(1, detail.get('price_cents', 1)):,} shares",
                        style=TEXT_DIM,
                    ),
                ),
            )
        for label, value in rows:
            table.add_row(Text(label, style=TEXT_DIM), value)
        return Group(Text("  YOUR POSITION", style=f"bold {ACCENT}"), table)

    def _news(self, state, detail: dict[str, Any]) -> Group:
        symbol = detail.get("symbol")
        items = [
            item
            for item in list(state.news)[::-1]
            if item.get("symbol") == symbol or item.get("sector") == detail.get("sector")
        ][:3]
        if not items:
            return Group(
                Text("\n  RELATED NEWS", style=f"bold {ACCENT}"),
                Text("  nothing recent", style=TEXT_DIM),
            )
        blocks = [Text("\n  RELATED NEWS", style=f"bold {ACCENT}")]
        for item in items:
            impact = item.get("impact_pct", 0.0)
            line = Text("  ")
            line.append(f"{impact:+.1f}%  ", style=UP if impact > 0 else DOWN)
            line.append(item["headline"], style=TEXT)
            blocks.append(line)
        return Group(*blocks)


# ---------------------------------------------------------------------------
# Portfolio
# ---------------------------------------------------------------------------


class PortfolioView(Container):
    def compose(self) -> ComposeResult:
        with Container(classes="panel column"):
            yield Static("", id="portfolio-summary")
            yield KeyedTable(id="portfolio-table")

    def on_mount(self) -> None:
        self.query_one("#portfolio-table", KeyedTable).setup(
            [
                ("symbol", "SYMBOL", 9),
                ("shares", "SHARES", 10),
                ("avg", "AVG COST", 12),
                ("price", "PRICE", 12),
                ("value", "MARKET VALUE", 15),
                ("pl", "UNREALISED P/L", 16),
                ("ret", "RETURN", 10),
                ("day", "TODAY", 10),
                ("weight", "WEIGHT", 14),
            ]
        )

    def refresh_view(self, state) -> None:
        portfolio = state.portfolio
        self.query_one("#portfolio-summary", Static).update(self._summary(state))
        positions = portfolio.get("positions", [])
        rows = []
        for position in positions:
            pl = position["unrealized_pl_cents"]
            style = UP if pl >= 0 else DOWN
            rows.append(
                (
                    position["symbol"],
                    [
                        Text(position["symbol"], style="bold white"),
                        Text(f"{position['quantity']:,}", style=TEXT),
                        Text(fmt_money(position["avg_cost_cents"]), style=TEXT_DIM),
                        Text(fmt_money(position["price_cents"]), style=TEXT),
                        Text(fmt_money(position["market_value_cents"]), style="bold white"),
                        Text(fmt_money(pl, sign=True), style=style),
                        Text(fmt_pct(position["return_pct"]), style=style),
                        Text(
                            f"{position['day_change_pct']:+.2f}%",
                            style=heat_style(position["day_change_pct"]),
                        ),
                        Text(
                            scale_bar(position["weight_pct"] / 100, 10),
                            style=ACCENT,
                        ),
                    ],
                )
            )
        self.query_one("#portfolio-table", KeyedTable).sync(rows)

    def _summary(self, state) -> Group:
        portfolio = state.portfolio
        if not portfolio:
            return Group(Text("\n  loading...", style=TEXT_DIM))

        table = Table.grid(padding=(0, 3))
        for _ in range(4):
            table.add_column(width=22)

        def cell(label: str, value: str, style: str) -> Text:
            body = Text()
            body.append(f"{label}\n", style=TEXT_DIM)
            body.append(value, style=style)
            return body

        total_pl = portfolio.get("total_pl_cents", 0)
        day_pl = portfolio.get("day_pl_cents", 0)
        unrealised = portfolio.get("unrealized_pl_cents", 0)
        realised = portfolio.get("realized_pl_cents", 0)

        table.add_row(
            cell("PORTFOLIO VALUE", fmt_money(portfolio.get("total_value_cents", 0)), "bold white"),
            cell("CASH / BUYING POWER", fmt_money(portfolio.get("cash_cents", 0)), TEXT),
            cell("HOLDINGS", fmt_money(portfolio.get("holdings_value_cents", 0)), TEXT),
            cell(
                "TOTAL RETURN",
                f"{fmt_money(total_pl, sign=True)}  ({fmt_pct(portfolio.get('total_return_pct', 0.0))})",
                UP if total_pl >= 0 else DOWN,
            ),
        )
        table.add_row(
            cell(
                "TODAY",
                f"{fmt_money(day_pl, sign=True)}  ({fmt_pct(portfolio.get('day_return_pct', 0.0))})",
                UP if day_pl >= 0 else DOWN,
            ),
            cell("UNREALISED", fmt_money(unrealised, sign=True), UP if unrealised >= 0 else DOWN),
            cell("REALISED", fmt_money(realised, sign=True), UP if realised >= 0 else DOWN),
            cell(
                "TRADES",
                f"{portfolio.get('total_trades', 0)}  "
                f"({portfolio.get('winning_trades', 0)}W / {portfolio.get('losing_trades', 0)}L)",
                TEXT,
            ),
        )
        heading = Text(" PORTFOLIO  ", style=f"bold {ACCENT}")
        heading.append(
            f"{len(portfolio.get('positions', []))} positions   Enter to open · B buy · S sell",
            style=TEXT_DIM,
        )
        return Group(heading, Text(""), table, Text(""))


# ---------------------------------------------------------------------------
# Watchlist
# ---------------------------------------------------------------------------


class WatchlistView(Container):
    def compose(self) -> ComposeResult:
        with Container(classes="panel column"):
            yield Static(
                Text.assemble(
                    (" WATCHLIST  ", f"bold {ACCENT}"),
                    ("+ add selected · - remove · Enter to open", TEXT_DIM),
                ),
            )
            yield KeyedTable(id="watchlist-table")

    def on_mount(self) -> None:
        self.query_one("#watchlist-table", KeyedTable).setup(
            [
                ("symbol", "SYMBOL", 9),
                ("name", "COMPANY", 28),
                ("sector", "SECTOR", 16),
                ("price", "PRICE", 13),
                ("change", "CHANGE", 12),
                ("range", "DAY RANGE", 20),
            ]
        )

    def refresh_view(self, state) -> None:
        rows = []
        for item in state.watchlist:
            symbol = item["symbol"]
            # Prefer the live tick over the snapshot the watchlist arrived with.
            live = state.stocks.get(symbol, item)
            change = live.get("change_pct", item.get("change_pct", 0.0))
            rows.append(
                (
                    symbol,
                    [
                        Text(symbol, style="bold white"),
                        Text(item["name"][:27], style=TEXT),
                        Text(item["sector"], style=sector_colour(item["sector"])),
                        Text(fmt_money(live.get("price_cents", 0)), style="bold white"),
                        Text(f"{change:+.2f}%", style=heat_style(change)),
                        day_range_marker(
                            live.get("day_low_cents", live.get("price_cents", 0)),
                            live.get("day_high_cents", live.get("price_cents", 0)),
                            live.get("price_cents", 0),
                            16,
                        ),
                    ],
                )
            )
        self.query_one("#watchlist-table", KeyedTable).sync(rows)


# ---------------------------------------------------------------------------
# Orders and trades
# ---------------------------------------------------------------------------


class OrdersView(Container):
    def compose(self) -> ComposeResult:
        with Container(classes="panel column"):
            yield Static(
                Text.assemble(
                    (" OPEN ORDERS  ", f"bold {ACCENT}"),
                    ("C to cancel the selected order", TEXT_DIM),
                ),
            )
            yield KeyedTable(id="orders-table")
            yield Static(Text(" TRADE HISTORY", style=f"bold {ACCENT}"))
            yield KeyedTable(id="trades-table")

    def on_mount(self) -> None:
        self.query_one("#orders-table", KeyedTable).setup(
            [
                ("symbol", "SYMBOL", 9),
                ("side", "SIDE", 7),
                ("type", "TYPE", 9),
                ("qty", "QTY", 10),
                ("filled", "FILLED", 10),
                ("limit", "LIMIT", 12),
                ("status", "STATUS", 18),
                ("placed", "PLACED", 21),
            ]
        )
        self.query_one("#trades-table", KeyedTable).setup(
            [
                ("time", "WHEN", 21),
                ("side", "SIDE", 7),
                ("symbol", "SYMBOL", 9),
                ("qty", "QTY", 10),
                ("price", "PRICE", 12),
                ("value", "VALUE", 14),
                ("pl", "REALISED P/L", 15),
            ]
        )

    def refresh_view(self, state) -> None:
        open_orders = state.open_orders()
        self.query_one("#orders-table", KeyedTable).sync(
            [
                (
                    order["id"],
                    [
                        Text(order["symbol"], style="bold white"),
                        Text(order["side"].upper(), style=UP if order["side"] == "buy" else DOWN),
                        Text(order["type"], style=TEXT_DIM),
                        Text(f"{order['quantity']:,}", style=TEXT),
                        Text(f"{order['filled_quantity']:,}", style=TEXT_DIM),
                        Text(
                            fmt_money(order["limit_price_cents"])
                            if order.get("limit_price_cents")
                            else "—",
                            style=TEXT,
                        ),
                        Text(order["status"].replace("_", " "), style=ACCENT),
                        Text(_short_time(order["created_at"]), style=TEXT_DIM),
                    ],
                )
                for order in open_orders
            ]
        )
        self.query_one("#trades-table", KeyedTable).sync(
            [
                (
                    trade["id"],
                    [
                        Text(_short_time(trade["created_at"]), style=TEXT_DIM),
                        Text(trade["side"].upper(), style=UP if trade["side"] == "buy" else DOWN),
                        Text(trade["symbol"], style="bold white"),
                        Text(f"{trade['quantity']:,}", style=TEXT),
                        Text(fmt_money(trade["price_cents"]), style=TEXT),
                        Text(fmt_money(trade["value_cents"]), style=TEXT),
                        Text(
                            fmt_money(trade["realized_pl_cents"], sign=True)
                            if trade["side"] == "sell"
                            else "—",
                            style=(
                                UP
                                if trade["realized_pl_cents"] > 0
                                else DOWN
                                if trade["realized_pl_cents"] < 0
                                else TEXT_DIM
                            ),
                        ),
                    ],
                )
                for trade in state.trades[:60]
            ]
        )


# ---------------------------------------------------------------------------
# Leaderboard
# ---------------------------------------------------------------------------


class LeaderboardView(Container):
    PERIODS = ("all_time", "season", "daily", "weekly", "monthly")
    LABELS = {
        "all_time": "ALL TIME",
        "season": "SEASON",
        "daily": "DAILY",
        "weekly": "WEEKLY",
        "monthly": "MONTHLY",
    }

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.period = "all_time"

    def compose(self) -> ComposeResult:
        with Container(classes="panel column"):
            yield Static("", id="leaderboard-heading")
            yield KeyedTable(id="leaderboard-table")

    def on_mount(self) -> None:
        self.query_one("#leaderboard-table", KeyedTable).setup(
            [
                ("rank", "RANK", 8),
                ("player", "PLAYER", 22),
                ("value", "PORTFOLIO VALUE", 18),
                ("pl", "PROFIT / LOSS", 18),
                ("ret", "RETURN", 12),
                ("bar", "", 16),
            ]
        )

    def cycle_period(self) -> str:
        index = (self.PERIODS.index(self.period) + 1) % len(self.PERIODS)
        self.period = self.PERIODS[index]
        return self.LABELS[self.period]

    def refresh_view(self, state) -> None:
        payload = state.leaderboard or {}
        boards = payload.get("boards", {})
        rows = boards.get(self.period, [])
        season = payload.get("season") or {}

        heading = Text(" LEADERBOARD  ", style=f"bold {ACCENT}")
        heading.append(self.LABELS[self.period], style=f"{INFO} reverse")
        heading.append("   G to switch board", style=TEXT_DIM)
        if season.get("name"):
            heading.append(f"   ·   Season {season['number']}: {season['name']}", style=ACCENT)
            if season.get("days_remaining") is not None:
                heading.append(f" ({season['days_remaining']}d left)", style=TEXT_DIM)
        if state.rank:
            heading.append(f"   ·   you: #{state.rank}", style=f"bold {ACCENT}")
        self.query_one("#leaderboard-heading", Static).update(heading)

        best = max((abs(row["return_pct"]) for row in rows), default=1.0) or 1.0
        table_rows = []
        for row in rows:
            is_you = row["username"] == state.username
            style = UP if row["pl_cents"] >= 0 else DOWN
            medal = {1: "🥇", 2: "🥈", 3: "🥉"}.get(row["rank"], f" {row['rank']}")
            table_rows.append(
                (
                    f"{self.period}:{row['username']}",
                    [
                        Text(f"{medal}", style=ACCENT if row["rank"] <= 3 else TEXT_DIM),
                        Text(
                            f"{'▶ ' if is_you else '  '}{row['username']}",
                            style=f"bold {ACCENT}" if is_you else "white",
                        ),
                        Text(fmt_money(row["value_cents"]), style="bold white"),
                        Text(fmt_money(row["pl_cents"], sign=True), style=style),
                        Text(fmt_pct(row["return_pct"]), style=style),
                        Text(
                            scale_bar(abs(row["return_pct"]) / best, 14),
                            style=style,
                        ),
                    ],
                )
            )
        self.query_one("#leaderboard-table", KeyedTable).sync(table_rows)


# ---------------------------------------------------------------------------
# News
# ---------------------------------------------------------------------------


class NewsView(Container):
    def compose(self) -> ComposeResult:
        with VerticalScroll(classes="panel column"):
            yield Static(
                Text.assemble(
                    (" NEWS & MARKET EVENTS  ", f"bold {ACCENT}"),
                    ("newest first", TEXT_DIM),
                ),
            )
            yield NewsPanel(limit=40, id="news-feed")

    def refresh_view(self, state) -> None:
        self.query_one("#news-feed", NewsPanel).render_state(state)


# ---------------------------------------------------------------------------
# Profile
# ---------------------------------------------------------------------------


class ProfileView(Container):
    def compose(self) -> ComposeResult:
        with VerticalScroll(classes="panel column"):
            yield Static("", id="profile-header")
            yield Static("", id="profile-curve")
            yield Static("", id="profile-achievements")

    def refresh_view(self, state) -> None:
        profile = state.profile
        if not profile:
            self.query_one("#profile-header", Static).update(
                Text("\n  loading profile...", style=TEXT_DIM)
            )
            return

        header = Table.grid(padding=(0, 3))
        for _ in range(4):
            header.add_column(width=22)

        def cell(label: str, value: str, style: str = TEXT) -> Text:
            body = Text()
            body.append(f"{label}\n", style=TEXT_DIM)
            body.append(value, style=style)
            return body

        total_pl = profile.get("total_pl_cents", 0)
        header.add_row(
            cell("PORTFOLIO VALUE", fmt_money(profile.get("total_value_cents", 0)), "bold white"),
            cell(
                "TOTAL RETURN",
                f"{fmt_money(total_pl, sign=True)} ({fmt_pct(profile.get('total_return_pct', 0.0))})",
                UP if total_pl >= 0 else DOWN,
            ),
            cell("RANK", f"#{profile.get('rank') or state.rank or '—'}", ACCENT),
            cell("POSITIONS", str(profile.get("position_count", 0))),
        )
        header.add_row(
            cell("TRADES", str(profile.get("total_trades", 0))),
            cell(
                "WIN RATE",
                f"{profile.get('win_rate_pct', 0.0):.0f}%  "
                f"({profile.get('winning_trades', 0)}W/{profile.get('losing_trades', 0)}L)",
            ),
            cell(
                "BEST TRADE",
                fmt_money(profile.get("best_trade_cents", 0), sign=True),
                UP,
            ),
            cell(
                "WORST TRADE",
                fmt_money(profile.get("worst_trade_cents", 0), sign=True),
                DOWN,
            ),
        )
        title = Text(f"\n  {profile.get('username', '')}", style=f"bold {ACCENT}")
        title.append(f"    member since {profile.get('member_since', '')[:10]}\n", style=TEXT_DIM)
        self.query_one("#profile-header", Static).update(Group(title, header, Text("")))

        curve = profile.get("equity_curve", [])
        if len(curve) >= 2:
            bars = [
                Bar(
                    open=point["value_cents"],
                    high=point["value_cents"],
                    low=point["value_cents"],
                    close=point["value_cents"],
                )
                for point in curve
            ]
            chart: Any = Group(
                Text("  EQUITY CURVE (daily close)", style=f"bold {ACCENT}"),
                PriceChart(bars, style="line", show_volume=False, height=12),
            )
        else:
            chart = Group(
                Text("  EQUITY CURVE", style=f"bold {ACCENT}"),
                Text(
                    "  Your equity curve appears after the first simulated day closes.",
                    style=TEXT_DIM,
                ),
            )
        self.query_one("#profile-curve", Static).update(chart)

        badges = profile.get("achievements", [])
        if badges:
            table = Table.grid(padding=(0, 2))
            table.add_column(width=4)
            table.add_column(width=22)
            table.add_column(width=44)
            for badge in badges:
                unlocked = badge["unlocked"]
                table.add_row(
                    Text("★" if unlocked else "☆", style=ACCENT if unlocked else CHART_GRID),
                    Text(badge["name"], style="bold white" if unlocked else TEXT_DIM),
                    Text(badge["description"], style=TEXT_DIM),
                )
            self.query_one("#profile-achievements", Static).update(
                Group(Text("\n  ACHIEVEMENTS", style=f"bold {ACCENT}"), table)
            )


def _short_time(iso: str) -> str:
    """``2026-09-08T16:35:19+00:00`` -> ``09-08 16:35:19``."""
    if not iso or "T" not in iso:
        return iso or ""
    date, _, rest = iso.partition("T")
    return f"{date[5:]} {rest[:8]}"
