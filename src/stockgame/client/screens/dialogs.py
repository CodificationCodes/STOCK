"""Modal dialogs: order entry, help and confirmation.

The order ticket shows an *estimate* and says so. The real fill price comes
back from the server, which applies spread and size-dependent slippage that
the client cannot predict and must not pretend to know.
"""

from __future__ import annotations

from typing import Any

from rich.align import Align
from rich.table import Table
from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Static

from stockgame.client.theme import ACCENT, DOWN, NEUTRAL, TEXT, TEXT_DIM, UP
from stockgame.shared.money import fmt_money
from stockgame.shared.validation import ValidationError, validate_price, validate_quantity


class TradeDialog(ModalScreen[dict | None]):
    """Order ticket. Returns the order payload to send, or ``None``."""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
        Binding("ctrl+s", "submit", "Place order", show=False),
        Binding("f2", "toggle_type", "Market/Limit"),
        Binding("f3", "fill_max", "Max size"),
    ]

    def __init__(
        self,
        symbol: str,
        side: str,
        *,
        price_cents: int,
        company_name: str = "",
        cash_cents: int = 0,
        shares_held: int = 0,
        quantity: int = 0,
    ) -> None:
        super().__init__()
        self.symbol = symbol
        self.side = side
        self.price_cents = price_cents
        # 'name' is reserved by Textual's Widget, hence the prefix.
        self.company_name = company_name
        self.cash_cents = cash_cents
        self.shares_held = shares_held
        self.order_type = "market"
        self.initial_quantity = quantity

    def compose(self) -> ComposeResult:
        is_buy = self.side == "buy"
        with Vertical(id="trade-card"):
            title = Text()
            title.append("BUY " if is_buy else "SELL ", style=f"bold {UP if is_buy else DOWN}")
            title.append(self.symbol, style="bold white")
            if self.company_name:
                title.append(f"  {self.company_name}", style=TEXT_DIM)
            yield Static(title, id="trade-title")
            yield Static(self._context_line(), id="trade-context")
            yield Input(
                placeholder="Quantity (shares)",
                id="quantity",
                type="integer",
                value=str(self.initial_quantity) if self.initial_quantity else "",
            )
            yield Input(placeholder="Limit price (leave blank for market)", id="limit")
            yield Static(self._preview(0, None), id="trade-preview")
            yield Static("", id="trade-error")
            with Horizontal(id="auth-buttons"):
                yield Button(
                    "Place order",
                    id="submit",
                    classes="-primary",
                )
                yield Button("Cancel", id="cancel")
            yield Static(
                Text("  F2 market/limit   ·   F3 max size   ·   Esc cancel", style=TEXT_DIM),
            )

    def on_mount(self) -> None:
        self.query_one("#quantity", Input).focus()

    # -- rendering ----------------------------------------------------------

    def _context_line(self) -> Text:
        # Not '_context': that name belongs to Textual's MessagePump.
        line = Text()
        line.append("  last ", style=TEXT_DIM)
        line.append(f"{fmt_money(self.price_cents)}", style="bold white")
        line.append("   buying power ", style=TEXT_DIM)
        line.append(fmt_money(self.cash_cents), style=TEXT)
        line.append("   held ", style=TEXT_DIM)
        line.append(f"{self.shares_held:,}", style=TEXT)
        line.append("\n")
        return line

    def _preview(self, quantity: int, limit_cents: int | None) -> Align:
        reference = limit_cents or self.price_cents
        gross = quantity * reference
        table = Table.grid(padding=(0, 2))
        table.add_column(width=18)
        table.add_column(width=18, justify="right")

        table.add_row(
            Text("Order type", style=TEXT_DIM),
            Text(self.order_type.upper(), style=ACCENT),
        )
        table.add_row(
            Text("Estimated price", style=TEXT_DIM),
            Text(fmt_money(reference), style=TEXT),
        )
        table.add_row(
            Text("Estimated value", style=TEXT_DIM),
            Text(fmt_money(gross), style="bold white"),
        )
        if self.side == "buy":
            remaining = self.cash_cents - gross
            table.add_row(
                Text("Cash after", style=TEXT_DIM),
                Text(fmt_money(remaining), style=TEXT if remaining >= 0 else DOWN),
            )
        else:
            table.add_row(
                Text("Shares after", style=TEXT_DIM),
                Text(
                    f"{self.shares_held - quantity:,}",
                    style=TEXT if quantity <= self.shares_held else DOWN,
                ),
            )
        table.add_row(
            Text("", style=TEXT_DIM),
            Text(
                "estimate only — the server sets the fill"
                if self.order_type == "market"
                else "fills at your limit or better",
                style=TEXT_DIM,
            ),
        )
        return Align.left(table)

    # -- interaction --------------------------------------------------------

    @on(Input.Changed)
    def _refresh(self) -> None:
        quantity, limit_cents = self._read(silent=True)
        self.order_type = "limit" if limit_cents else "market"
        self.query_one("#trade-preview", Static).update(self._preview(quantity or 0, limit_cents))

    @on(Input.Submitted)
    def _on_enter(self, event: Input.Submitted) -> None:
        if event.input.id == "quantity":
            self.query_one("#limit", Input).focus()
        else:
            self.action_submit()

    @on(Button.Pressed, "#submit")
    def _on_submit(self) -> None:
        self.action_submit()

    @on(Button.Pressed, "#cancel")
    def _on_cancel(self) -> None:
        self.action_cancel()

    def action_toggle_type(self) -> None:
        limit = self.query_one("#limit", Input)
        if limit.value:
            limit.value = ""
        else:
            limit.value = f"{self.price_cents / 100:.2f}"
        self._refresh()

    def action_fill_max(self) -> None:
        """Fill in the largest size the account can support."""
        quantity_input = self.query_one("#quantity", Input)
        if self.side == "sell":
            quantity_input.value = str(self.shares_held)
        else:
            _, limit_cents = self._read(silent=True)
            price = limit_cents or self.price_cents
            # Leave a 1% margin: a market order's fill can land above the last
            # print, and an order the server rejects helps nobody.
            affordable = int(self.cash_cents * 0.99 // price) if price else 0
            quantity_input.value = str(max(0, affordable))
        self._refresh()

    def action_cancel(self) -> None:
        self.dismiss(None)

    def action_submit(self) -> None:
        quantity, limit_cents = self._read(silent=False)
        if not quantity:
            return
        payload: dict[str, Any] = {
            "symbol": self.symbol,
            "side": self.side,
            "type": "limit" if limit_cents else "market",
            "quantity": quantity,
        }
        if limit_cents:
            payload["limit_price"] = limit_cents / 100
        self.dismiss(payload)

    def _read(self, *, silent: bool) -> tuple[int | None, int | None]:
        error = self.query_one("#trade-error", Static)
        raw_quantity = self.query_one("#quantity", Input).value.strip()
        raw_limit = self.query_one("#limit", Input).value.strip()

        quantity: int | None = None
        limit_cents: int | None = None
        try:
            if raw_quantity:
                quantity = validate_quantity(raw_quantity)
            elif not silent:
                raise ValidationError("Enter a quantity.")
            if raw_limit:
                limit_cents = round(validate_price(raw_limit) * 100)
        except ValidationError as exc:
            if not silent:
                error.update(Text(f"  {exc}", style=DOWN))
            return None, limit_cents

        if not silent and quantity is not None:
            if self.side == "sell" and quantity > self.shares_held:
                error.update(Text(f"  You only hold {self.shares_held:,} shares.", style=DOWN))
                return None, limit_cents
            cost = quantity * (limit_cents or self.price_cents)
            if self.side == "buy" and cost > self.cash_cents:
                error.update(
                    Text(
                        f"  That needs {fmt_money(cost)}; you have {fmt_money(self.cash_cents)}.",
                        style=DOWN,
                    )
                )
                return None, limit_cents
            error.update("")
        return quantity, limit_cents


class ConfirmDialog(ModalScreen[bool]):
    BINDINGS = [
        Binding("escape", "no", "No"),
        Binding("y", "yes", "Yes"),
        Binding("n", "no", "No"),
    ]

    def __init__(self, title: str, body: str) -> None:
        super().__init__()
        self.title_text = title
        self.body_text = body

    def compose(self) -> ComposeResult:
        with Vertical(id="confirm-card"):
            yield Static(Text(self.title_text, style=f"bold {ACCENT}"))
            yield Static(Text(f"\n{self.body_text}\n", style=TEXT))
            with Horizontal(id="auth-buttons"):
                yield Button("Yes", id="yes", classes="-primary")
                yield Button("No", id="no")

    @on(Button.Pressed, "#yes")
    def action_yes(self) -> None:
        self.dismiss(True)

    @on(Button.Pressed, "#no")
    def action_no(self) -> None:
        self.dismiss(False)


HELP_SECTIONS: tuple[tuple[str, tuple[tuple[str, str], ...]], ...] = (
    (
        "NAVIGATION",
        (
            ("M", "Market overview"),
            ("T", "Ticker / stock detail"),
            ("P", "Portfolio"),
            ("W", "Watchlist"),
            ("O", "Orders and trade history"),
            ("L", "Leaderboard"),
            ("N", "News feed"),
            ("U", "Your profile"),
            ("Tab / ↑ ↓", "Move between rows"),
            ("Enter", "Open the selected symbol, or a rival's book in LEAGUE"),
            ("Esc", "Back / close"),
        ),
    ),
    (
        "TRADING",
        (
            ("B", "Buy the selected symbol"),
            ("S", "Sell the selected symbol"),
            ("C", "Cancel the selected open order"),
            ("F2 (in ticket)", "Toggle market / limit"),
            ("F3 (in ticket)", "Maximum size"),
        ),
    ),
    (
        "CHARTS",
        (
            ("1 2 3 4 5", "1D · 1W · 1M · 3M · 1Y"),
            ("V", "Toggle candles / line"),
            ("+ / -", "Add or remove from the watchlist"),
        ),
    ),
    (
        "GENERAL",
        (
            ("R", "Refresh everything"),
            ("?", "This help"),
            ("Ctrl+P", "Command palette"),
            ("Q", "Quit"),
        ),
    ),
)


class HelpDialog(ModalScreen[None]):
    BINDINGS = [
        Binding("escape", "close", "Close"),
        Binding("question_mark", "close", "Close", show=False),
        Binding("q", "close", "Close", show=False),
    ]

    def compose(self) -> ComposeResult:
        table = Table.grid(padding=(0, 3))
        table.add_column(width=18)
        table.add_column(width=30)
        table.add_column(width=18)
        table.add_column(width=30)

        columns: list[list[Text]] = [[], []]
        for index, (heading, rows) in enumerate(HELP_SECTIONS):
            target = columns[index % 2]
            block = Text()
            block.append(f"{heading}\n", style=f"bold {ACCENT}")
            for key, description in rows:
                block.append(f"  {key:<16}", style="bold white")
                block.append(f"{description}\n", style=NEUTRAL)
            target.append(block)

        grid = Table.grid(padding=(1, 4))
        grid.add_column()
        grid.add_column()
        for left, right in zip(columns[0], columns[1] + [Text("")], strict=False):
            grid.add_row(left, right)

        with Vertical(id="help-card"):
            yield Static(Text("KEYBOARD SHORTCUTS", style=f"bold {ACCENT}"))
            yield Static(grid)
            yield Static(
                Text(
                    "\nAll prices, balances and fills are computed by the server.\n"
                    "This is a simulation using virtual currency only.",
                    style=TEXT_DIM,
                )
            )

    def action_close(self) -> None:
        self.dismiss(None)


class PlayerDialog(ModalScreen[None]):
    """Another player's book, opened with Enter from the leaderboard.

    The server decides what a rival may see: position sizes and returns, but
    never cash or cost basis. This just renders whatever came back.
    """

    BINDINGS = [
        Binding("escape", "close", "Close"),
        Binding("q", "close", "Close", show=False),
        Binding("enter", "close", "Close", show=False),
    ]

    def __init__(self, profile: dict[str, Any]) -> None:
        super().__init__()
        self.profile = profile

    def compose(self) -> ComposeResult:
        with Vertical(id="player-card"):
            yield Static(self._header())
            yield Static(self._positions())
            yield Static(Text("\n  Esc to close", style=TEXT_DIM))

    def _header(self) -> Text:
        profile = self.profile
        header = Text()
        header.append(f"{profile.get('username', '?')}\n", style=f"bold {ACCENT}")
        value = profile.get("total_value_cents", 0)
        pl = profile.get("total_pl_cents", 0)
        header.append("  VALUE ", style=TEXT_DIM)
        header.append(f"{fmt_money(value)}", style="bold white")
        header.append("   P/L ", style=TEXT_DIM)
        header.append(fmt_money(pl, sign=True), style=UP if pl >= 0 else DOWN)
        header.append("   RETURN ", style=TEXT_DIM)
        ret = profile.get("total_return_pct", 0.0)
        header.append(f"{ret:+.2f}%", style=UP if ret >= 0 else DOWN)
        header.append(f"\n  {profile.get('total_trades', 0)} trades", style=TEXT_DIM)
        return header

    def _positions(self) -> Any:
        positions = self.profile.get("positions") or []
        if not positions:
            return Text("\n  Holding nothing but cash.\n", style=TEXT_DIM)
        table = Table.grid(padding=(0, 2))
        table.add_column(width=8)
        table.add_column(width=10, justify="right")
        table.add_column(width=14, justify="right")
        table.add_column(width=10, justify="right")
        table.add_row(
            Text("SYMBOL", style=NEUTRAL),
            Text("SHARES", style=NEUTRAL),
            Text("VALUE", style=NEUTRAL),
            Text("RETURN", style=NEUTRAL),
        )
        for row in positions:
            ret = row.get("return_pct", 0.0)
            table.add_row(
                Text(row["symbol"], style="bold white"),
                Text(f"{row['quantity']:,}", style=TEXT),
                Text(fmt_money(row["market_value_cents"]), style="bold white"),
                Text(f"{ret:+.2f}%", style=UP if ret >= 0 else DOWN),
            )
        return table

    def action_close(self) -> None:
        self.dismiss(None)
