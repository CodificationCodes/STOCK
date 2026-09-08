"""Welcome, login and register screens -- the first thing a player sees."""

from __future__ import annotations

from typing import Any

from rich.align import Align
from rich.text import Text
from textual import on, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, Vertical
from textual.screen import Screen
from textual.widgets import Button, Input, Static

from stockgame.client.theme import ACCENT, DOWN, NEUTRAL, TEXT_DIM, UP
from stockgame.client.transport import AuthClient, ClientError
from stockgame.shared.money import fmt_money
from stockgame.shared.validation import (
    USERNAME_MAX,
    USERNAME_MIN,
    ValidationError,
    validate_password,
    validate_username,
)

LOGO = r"""
 ███████╗████████╗ ██████╗  ██████╗██╗  ██╗
 ██╔════╝╚══██╔══╝██╔═══██╗██╔════╝██║ ██╔╝
 ███████╗   ██║   ██║   ██║██║     █████╔╝
 ╚════██║   ██║   ██║   ██║██║     ██╔═██╗
 ███████║   ██║   ╚██████╔╝╚██████╗██║  ██╗
 ╚══════╝   ╚═╝    ╚═════╝  ╚═════╝╚═╝  ╚═╝
"""


class WelcomeScreen(Screen):
    """The launch menu."""

    BINDINGS = [
        Binding("l", "choose('login')", "Login"),
        Binding("r", "choose('register')", "Register"),
        Binding("s", "change_server", "Server"),
        Binding("q", "quit_app", "Quit"),
        Binding("escape", "quit_app", "Quit", show=False),
    ]

    def __init__(self, server: str, info: dict[str, Any] | None = None) -> None:
        super().__init__()
        self.server = server
        self.info = info or {}

    def compose(self) -> ComposeResult:
        with Container(id="auth-wrapper"), Vertical(id="auth-card"):
            yield Static(Align.center(Text(LOGO.strip("\n"), style=ACCENT)), id="auth-logo")
            yield Static(
                Align.center(Text("STOCK MARKET SIMULATOR", style=f"bold {ACCENT}")),
                id="auth-title",
            )
            yield Static(self._subtitle(), id="auth-subtitle")
            yield Static(self._menu(), id="auth-menu")
            yield Static(self._footer(), id="auth-message")

    def _subtitle(self) -> Align:
        if self.info:
            body = Text()
            body.append(f"{self.info.get('symbols', 0)} listed companies", style=NEUTRAL)
            body.append("  ·  ", style=TEXT_DIM)
            online = self.info.get("players_online", 0)
            body.append(f"{online} player{'s' if online != 1 else ''} online", style=NEUTRAL)
            season = self.info.get("season") or {}
            if season.get("name"):
                body.append("  ·  ", style=TEXT_DIM)
                body.append(f"Season {season['number']}: {season['name']}", style=ACCENT)
            return Align.center(body)
        return Align.center(Text("Connecting to the exchange...", style=TEXT_DIM))

    def _menu(self) -> Align:
        body = Text()
        for key, label in (
            ("L", "Login"),
            ("R", "Register"),
            ("S", "Change server"),
            ("Q", "Quit"),
        ):
            body.append("     [ ", style=TEXT_DIM)
            body.append(key, style=f"bold {ACCENT}")
            body.append(" ]  ", style=TEXT_DIM)
            body.append(f"{label}\n", style="white")
        return Align.center(body)

    def _footer(self) -> Align:
        body = Text()
        body.append(f"server  {self.server}\n", style=TEXT_DIM)
        body.append("Virtual currency only. No real money is involved.", style=TEXT_DIM)
        return Align.center(body)

    def on_mount(self) -> None:
        if not self.info:
            self.fetch_info()

    @work(exclusive=True)
    async def fetch_info(self) -> None:
        try:
            self.info = await AuthClient(self.server).info()
        except ClientError as exc:
            body = Text()
            body.append(f"{exc.message}\n", style=DOWN)
            body.append("Press S to point the client at a different server.", style=TEXT_DIM)
            self.query_one("#auth-subtitle", Static).update(Align.center(body))
            return
        self.query_one("#auth-subtitle", Static).update(self._subtitle())

    def action_choose(self, mode: str) -> None:
        self.app.push_screen(CredentialsScreen(self.server, mode))

    def action_change_server(self) -> None:
        self.app.push_screen(ServerScreen(self.server))

    def action_quit_app(self) -> None:
        self.app.exit()


class CredentialsScreen(Screen):
    """Shared login/register form."""

    BINDINGS = [
        Binding("escape", "back", "Back"),
        Binding("ctrl+s", "submit", "Submit", show=False),
    ]

    def __init__(self, server: str, mode: str) -> None:
        super().__init__()
        self.server = server
        self.mode = mode  # "login" | "register"
        self.busy = False

    def compose(self) -> ComposeResult:
        registering = self.mode == "register"
        with Container(id="auth-wrapper"), Vertical(id="auth-card"):
            yield Static(
                Align.center(
                    Text("CREATE ACCOUNT" if registering else "SIGN IN", style=f"bold {ACCENT}")
                ),
                id="auth-title",
            )
            yield Static(
                Align.center(
                    Text(
                        f"{USERNAME_MIN}-{USERNAME_MAX} characters, letters, numbers, underscore"
                        if registering
                        else self.server,
                        style=TEXT_DIM,
                    )
                ),
                id="auth-subtitle",
            )
            yield Input(placeholder="Username", id="username", max_length=USERNAME_MAX)
            yield Input(placeholder="Password", password=True, id="password", max_length=128)
            if registering:
                yield Input(
                    placeholder="Confirm password", password=True, id="confirm", max_length=128
                )
            with Horizontal(id="auth-buttons"):
                yield Button(
                    "Create account" if registering else "Sign in",
                    id="submit",
                    variant="primary",
                    classes="-primary",
                )
                yield Button("Back", id="back")
            yield Static("", id="auth-message")

    def on_mount(self) -> None:
        username = self.query_one("#username", Input)
        remembered = getattr(self.app, "config", None)
        if remembered is not None and remembered.last_username and self.mode == "login":
            username.value = remembered.last_username
            self.query_one("#password", Input).focus()
            return
        username.focus()

    @on(Input.Submitted)
    def _advance(self, event: Input.Submitted) -> None:
        """Enter moves to the next field, and submits from the last one."""
        order = ["username", "password"] + (["confirm"] if self.mode == "register" else [])
        try:
            index = order.index(event.input.id or "")
        except ValueError:
            return
        if index + 1 < len(order):
            self.query_one(f"#{order[index + 1]}", Input).focus()
        else:
            self.action_submit()

    @on(Button.Pressed, "#submit")
    def _on_submit(self) -> None:
        self.action_submit()

    @on(Button.Pressed, "#back")
    def _on_back(self) -> None:
        self.action_back()

    def action_back(self) -> None:
        self.app.pop_screen()

    def action_submit(self) -> None:
        if self.busy:
            return
        username = self.query_one("#username", Input).value.strip()
        password = self.query_one("#password", Input).value

        try:
            # Client-side validation is purely for fast feedback; the server
            # re-runs exactly these rules before it trusts anything.
            validate_username(username)
            if self.mode == "register":
                validate_password(password)
                if password != self.query_one("#confirm", Input).value:
                    raise ValidationError("Passwords do not match.")
        except ValidationError as exc:
            self._message(str(exc), error=True)
            return

        self.busy = True
        self._message("Contacting the exchange...", error=False)
        self.authenticate(username, password)

    @work(exclusive=True)
    async def authenticate(self, username: str, password: str) -> None:
        client = AuthClient(self.server)
        try:
            account = (
                await client.register(username, password)
                if self.mode == "register"
                else await client.login(username, password)
            )
        except ClientError as exc:
            self.busy = False
            self._message(exc.message, error=True)
            hint = {
                "unreachable": "Check the address with S on the previous screen.",
                "rate_limited": "The server is throttling repeated attempts.",
            }.get(exc.code)
            if hint:
                self.notify(hint, severity="warning")
            return
        except Exception as exc:  # pragma: no cover - defensive
            self.busy = False
            self._message(f"Unexpected error: {exc}", error=True)
            return

        self.busy = False
        await self.app.on_authenticated(account)

    def _message(self, text: str, *, error: bool) -> None:
        body = Text(text, style=DOWN if error else TEXT_DIM)
        self.query_one("#auth-message", Static).update(Align.center(body))


class ServerScreen(Screen):
    """Change which server the client talks to."""

    BINDINGS = [Binding("escape", "back", "Back")]

    def __init__(self, server: str) -> None:
        super().__init__()
        self.server = server

    def compose(self) -> ComposeResult:
        with Container(id="auth-wrapper"), Vertical(id="auth-card"):
            yield Static(Align.center(Text("SERVER", style=f"bold {ACCENT}")), id="auth-title")
            yield Static(
                Align.center(
                    Text(
                        "Host, host:port or full URL.\n"
                        "e.g. stocks.example.com  ·  127.0.0.1:8765",
                        style=TEXT_DIM,
                    )
                ),
                id="auth-subtitle",
            )
            yield Input(value=self.server, id="server", placeholder="127.0.0.1:8765")
            with Horizontal(id="auth-buttons"):
                yield Button("Save", id="save", classes="-primary")
                yield Button("Back", id="back")
            yield Static("", id="auth-message")

    def on_mount(self) -> None:
        self.query_one("#server", Input).focus()

    @on(Input.Submitted)
    @on(Button.Pressed, "#save")
    def _save(self) -> None:
        value = self.query_one("#server", Input).value.strip()
        if not value:
            return
        self.app.set_server(value)
        self.app.pop_screen()

    @on(Button.Pressed, "#back")
    def action_back(self) -> None:
        self.app.pop_screen()


class NewAccountScreen(Screen):
    """Shown once, immediately after a successful registration."""

    BINDINGS = [
        Binding("enter", "continue_", "Continue"),
        Binding("space", "continue_", "Continue", show=False),
    ]

    def __init__(self, username: str, starting_cash_cents: int) -> None:
        super().__init__()
        self.username = username
        self.starting_cash_cents = starting_cash_cents

    def compose(self) -> ComposeResult:
        cash = fmt_money(self.starting_cash_cents)
        body = Text()
        body.append("\n  Welcome, ", style="white")
        body.append(f"{self.username}\n\n", style=f"bold {ACCENT}")
        body.append(f"  {'Starting cash':<22}", style=TEXT_DIM)
        body.append(f"{cash:>16}\n", style=UP)
        body.append(f"  {'Portfolio value':<22}", style=TEXT_DIM)
        body.append(f"{cash:>16}\n", style="white")
        body.append(f"  {'Open positions':<22}", style=TEXT_DIM)
        body.append(f"{'0':>16}\n\n", style="white")
        body.append("  You are competing against every other player\n", style=TEXT_DIM)
        body.append("  on one shared, always-running market.\n\n", style=TEXT_DIM)
        body.append("  Press ", style=TEXT_DIM)
        body.append("Enter", style=f"bold {ACCENT}")
        body.append(" to open the trading desk", style=TEXT_DIM)

        with Container(id="auth-wrapper"), Vertical(id="auth-card"):
            yield Static(
                Align.center(Text("ACCOUNT CREATED", style=f"bold {ACCENT}")), id="auth-title"
            )
            yield Static(body, id="auth-menu")

    def action_continue_(self) -> None:
        self.dismiss(True)
