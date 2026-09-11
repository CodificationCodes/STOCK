"""Chrome widgets: the header, navigation, status bar and ticker tape.

These are all thin :class:`Static` subclasses that re-render from
:class:`~stockgame.client.state.ClientState` on demand. Textual only repaints
the widgets whose content actually changed, so a 2-second price tick does not
redraw the screen.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any

from rich.console import Group
from rich.table import Table
from rich.text import Text
from textual.widgets import Static

from stockgame.client.theme import (
    ACCENT,
    DOWN,
    INFO,
    NEUTRAL,
    TEXT,
    TEXT_DIM,
    UP,
    WARNING,
    sector_colour,
)
from stockgame.client.widgets.chart import scale_bar
from stockgame.shared.money import fmt_money, fmt_pct


def format_clock(raw: Any) -> str:
    """``"14:07"`` in the player's own timezone. Empty if unknown.

    Server timestamps are UTC ISO strings; everyone reads them on their own
    wall clock, so a Sydney player and a London player see their own time.
    """
    if not isinstance(raw, str) or not raw:
        return ""
    try:
        moment = datetime.fromisoformat(raw)
    except ValueError:
        return ""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return f"{moment.astimezone():%H:%M}"


def format_reopen(raw: Any) -> str:
    """``"opens 09:00"``, in the player's own timezone. Empty if unknown."""
    if not isinstance(raw, str) or not raw:
        return ""
    try:
        moment = datetime.fromisoformat(raw)
    except ValueError:
        return ""
    local = moment.astimezone()
    if local.date() == datetime.now().astimezone().date():
        return f"opens {local:%H:%M}"
    return f"opens {local:%a %H:%M}"


REGIME_LABELS = {
    "euphoria": ("EUPHORIA", UP),
    "bull": ("BULL", UP),
    "neutral": ("NEUTRAL", NEUTRAL),
    "bear": ("BEAR", DOWN),
    "panic": ("PANIC", f"bold {DOWN}"),
}

NAV_ITEMS = (
    ("market", "M", "MARKET"),
    ("stock", "T", "TICKER"),
    ("portfolio", "P", "PORTFOLIO"),
    ("watchlist", "W", "WATCHLIST"),
    ("orders", "O", "ORDERS"),
    ("leaderboard", "L", "LEAGUE"),
    ("news", "N", "NEWS"),
    ("profile", "U", "PROFILE"),
)


class TopBar(Static):
    """Market-wide context: the index, regime, simulated clock and season."""

    def render_state(self, state) -> None:
        status = state.market_status or {}
        line = Text()
        line.append(" STOCKGAME ", style=f"bold {ACCENT}")
        line.append("│ ", style=TEXT_DIM)

        index = status.get("index_value", 0)
        change = status.get("index_change_pct", 0.0)
        line.append("SGX ", style=TEXT_DIM)
        line.append(f"{index / 100:,.2f} ", style="bold white")
        line.append(_change_text(change))
        line.append("  │ ", style=TEXT_DIM)

        label, style = REGIME_LABELS.get(status.get("regime", "neutral"), ("—", NEUTRAL))
        line.append(label, style=style)
        line.append("  │ ", style=TEXT_DIM)

        market_state = status.get("status", "open")
        line.append(
            "● OPEN" if market_state == "open" else f"● {market_state.upper()}",
            style=UP if market_state == "open" else WARNING,
        )
        reopen = format_reopen(status.get("next_open"))
        if reopen:
            line.append(f" {reopen}", style=TEXT_DIM)
        line.append("  │ ", style=TEXT_DIM)
        line.append(f"DAY {status.get('day_index', 0)}", style=TEXT_DIM)

        elapsed = status.get("day_elapsed", 0)
        total = status.get("day_seconds", 1) or 1
        line.append(" ", style=TEXT_DIM)
        line.append(scale_bar(min(1.0, elapsed / total), 8), style=ACCENT)

        season = state.season or {}
        if season.get("name"):
            line.append("  │ ", style=TEXT_DIM)
            line.append(f"S{season['number']} {season['name']}", style=ACCENT)

        line.append("  │ ", style=TEXT_DIM)
        line.append(f"{state.players_online} online", style=INFO)
        self.update(line)


class NavBar(Static):
    """Tab strip with the shortcut key called out on each entry."""

    def render_state(self, active: str) -> None:
        line = Text(" ")
        for key, shortcut, label in NAV_ITEMS:
            is_active = key == active
            line.append(" ")
            line.append(f"{shortcut}", style=f"bold {ACCENT}" if is_active else TEXT_DIM)
            line.append(" ")
            line.append(
                label,
                style=f"bold {TEXT} reverse" if is_active else NEUTRAL,
            )
            line.append(" ")
            line.append("│", style=TEXT_DIM)
        line.append("  ? help", style=TEXT_DIM)
        self.update(line)


class StatusBar(Static):
    """The player's own numbers, always visible."""

    def render_state(self, state) -> None:
        portfolio = state.portfolio
        line = Text()
        if not portfolio:
            line.append(" loading portfolio...", style=TEXT_DIM)
            self.update(line)
            return

        line.append(" CASH ", style=TEXT_DIM)
        line.append(f"{fmt_money(portfolio.get('cash_cents', 0)):>13}", style="white")
        line.append("  VALUE ", style=TEXT_DIM)
        line.append(f"{fmt_money(portfolio.get('total_value_cents', 0)):>13}", style="bold white")

        line.append("  TODAY ", style=TEXT_DIM)
        day_pl = portfolio.get("day_pl_cents", 0)
        line.append(
            f"{fmt_money(day_pl, sign=True)} ({fmt_pct(portfolio.get('day_return_pct', 0.0))})",
            style=UP if day_pl >= 0 else DOWN,
        )

        line.append("  TOTAL ", style=TEXT_DIM)
        total_pl = portfolio.get("total_pl_cents", 0)
        line.append(
            f"{fmt_money(total_pl, sign=True)} ({fmt_pct(portfolio.get('total_return_pct', 0.0))})",
            style=UP if total_pl >= 0 else DOWN,
        )

        if state.rank:
            line.append("  RANK ", style=TEXT_DIM)
            line.append(f"#{state.rank}", style=f"bold {ACCENT}")

        line.append("  │ ", style=TEXT_DIM)
        line.append(*_connection_indicator(state))
        self.update(line)


class TickerTape(Static):
    """A smooth scrolling price marquee along the bottom edge."""

    SPEED = 2  # characters per refresh

    def __init__(self, **kwargs: Any) -> None:
        super().__init__("", **kwargs)
        self._segments: list[tuple[str, str]] = []
        self._plain_length = 0
        self._offset = 0

    def rebuild(self, state) -> None:
        """Recompute the marquee content from current prices."""
        segments: list[tuple[str, str]] = []
        rows = sorted(state.stocks.values(), key=lambda row: row["symbol"])
        for row in rows:
            change = row.get("change_pct", 0.0)
            arrow = "▲" if change > 0 else ("▼" if change < 0 else "•")
            style = UP if change > 0 else (DOWN if change < 0 else NEUTRAL)
            segments.append((f" {row['symbol']} ", "bold white"))
            segments.append((f"{row['price_cents'] / 100:,.2f} ", TEXT))
            segments.append((f"{arrow}{abs(change):.2f}%", style))
            segments.append(("   ·  ", TEXT_DIM))
        self._segments = segments
        self._plain_length = sum(len(text) for text, _ in segments)

    def advance(self) -> None:
        if not self._plain_length:
            return
        self._offset = (self._offset + self.SPEED) % self._plain_length
        self.update(self._window(self._offset, max(10, self.size.width or 80)))

    def _window(self, offset: int, width: int) -> Text:
        """Slice ``width`` characters out of the (conceptually infinite) tape."""
        text = Text(no_wrap=True, overflow="crop")
        remaining = width
        position = 0
        index = 0
        # Two passes at most: walk from the offset, wrapping once at the end.
        for _ in range(2):
            for segment_text, style in self._segments[index:]:
                length = len(segment_text)
                if position + length <= offset:
                    position += length
                    continue
                start = max(0, offset - position)
                chunk = segment_text[start : start + remaining]
                text.append(chunk, style=style)
                remaining -= len(chunk)
                position += length
                if remaining <= 0:
                    return text
            offset, position, index = 0, 0, 0
        return text


class SectorPanel(Static):
    """Sector performance as a diverging bar chart."""

    def render_state(self, state) -> None:
        table = Table.grid(padding=(0, 1))
        table.add_column(width=15)
        table.add_column(width=8, justify="right")
        table.add_column(width=12)
        for row in state.sectors:
            change = row.get("change_pct", 0.0)
            magnitude = min(1.0, abs(change) / 3.0)
            bar = scale_bar(magnitude, 10)
            table.add_row(
                Text(row["sector"], style=sector_colour(row["sector"])),
                Text(f"{change:+.2f}%", style=UP if change >= 0 else DOWN),
                Text(bar, style=UP if change >= 0 else DOWN),
            )
        self.update(table)


class TradeTapePanel(Static):
    """Live prints from other players -- the most direct 'others are here' cue."""

    def render_state(self, state) -> None:
        if not state.tape:
            self.update(Text("\n  waiting for the first trade...", style=TEXT_DIM))
            return
        table = Table.grid(padding=(0, 1))
        table.add_column(width=5)
        table.add_column(width=11)
        table.add_column(width=4)
        table.add_column(width=6)
        table.add_column(width=7, justify="right")
        table.add_column(width=9, justify="right")
        for print_ in list(state.tape)[-14:][::-1]:
            is_buy = print_["side"] == "buy"
            table.add_row(
                Text(format_clock(print_.get("at")), style=TEXT_DIM),
                Text(
                    print_["username"][:11],
                    style=ACCENT if print_["username"] == state.username else TEXT,
                ),
                Text("BUY" if is_buy else "SELL", style=UP if is_buy else DOWN),
                Text(print_["symbol"], style="bold white"),
                Text(f"{print_['quantity']:,}", style=TEXT_DIM),
                Text(fmt_money(print_["price_cents"]), style=TEXT),
            )
        self.update(table)


class NewsPanel(Static):
    """Headline feed with the price impact attached."""

    def __init__(self, limit: int = 8, **kwargs: Any) -> None:
        super().__init__("", **kwargs)
        self.limit = limit

    def render_state(self, state) -> None:
        if not state.news:
            self.update(Text("\n  no headlines yet", style=TEXT_DIM))
            return
        blocks = []
        for item in list(state.news)[-self.limit :][::-1]:
            impact = item.get("impact_pct", 0.0)
            style = UP if impact > 0 else (DOWN if impact < 0 else NEUTRAL)
            headline = Text(no_wrap=True, overflow="ellipsis")
            tag = item.get("symbol") or item.get("sector") or "MARKET"
            headline.append(f"{format_clock(item.get('at')):<6}", style=TEXT_DIM)
            headline.append(f"{tag:<12}", style=ACCENT)
            headline.append(f"{impact:+.1f}%  ", style=style)
            headline.append(item["headline"], style="bold white")
            body = Text(
                f"                  {item.get('body', '')}",
                style=TEXT_DIM,
                no_wrap=True,
                overflow="ellipsis",
            )
            blocks.extend([headline, body])
        self.update(Group(*blocks))


class ChatPanel(Static):
    """The room. Newest at the bottom, like every chat ever made."""

    def __init__(self, limit: int = 8, **kwargs: Any) -> None:
        super().__init__("", **kwargs)
        self.limit = limit

    def render_state(self, state) -> None:
        if not state.chat:
            self.update(Text("\n  nobody has said anything yet", style=TEXT_DIM))
            return
        lines = []
        for item in list(state.chat)[-self.limit :]:
            line = Text(no_wrap=True, overflow="ellipsis")
            line.append(f"{format_clock(item.get('at')):<6}", style=TEXT_DIM)
            is_you = item.get("username") == state.username
            line.append(f"{item.get('username', '?')[:11]:<12}", style=ACCENT if is_you else INFO)
            line.append(item.get("body", ""), style=TEXT)
            lines.append(line)
        self.update(Group(*lines))


class ClockMixin:
    """Formats the wall-clock time for the header."""

    @staticmethod
    def now() -> str:
        return time.strftime("%H:%M:%S")


def _change_text(change_pct: float) -> Text:
    if change_pct > 0:
        return Text(f"▲ {change_pct:+.2f}%", style=UP)
    if change_pct < 0:
        return Text(f"▼ {change_pct:+.2f}%", style=DOWN)
    return Text("• 0.00%", style=NEUTRAL)


def _connection_indicator(state) -> tuple[str, str]:
    mapping = {
        "connected": ("● live", UP),
        "connecting": ("○ connecting", WARNING),
        "reconnecting": ("○ reconnecting", WARNING),
        "error": ("● offline", DOWN),
    }
    return mapping.get(state.connection_status, ("○ ...", NEUTRAL))
