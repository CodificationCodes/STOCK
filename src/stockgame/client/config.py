"""Client configuration at ``~/.stockgame/config.toml``.

Stores the server address (so it need only be typed once) and the session
token per server, so switching between a local dev server and a remote one
does not log you out of either.

The file is created with ``0600`` permissions because it contains tokens.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import tomli_w

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - 3.10 only
    import tomli as tomllib

DEFAULT_SERVER = "http://127.0.0.1:8765"
CONFIG_FILENAME = "config.toml"


def config_dir() -> Path:
    """``~/.stockgame``, or ``$STOCKGAME_CONFIG_DIR`` when set (used by tests)."""
    override = os.environ.get("STOCKGAME_CONFIG_DIR")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".stockgame"


@dataclass
class ClientConfig:
    server: str = DEFAULT_SERVER
    #: Session tokens keyed by normalised server URL.
    tokens: dict[str, str] = field(default_factory=dict)
    #: Last username used, to prefill the login form.
    last_username: str = ""
    chart_style: str = "candles"  # "candles" or "line"
    default_timeframe: str = "1D"
    show_tape: bool = True
    confirm_orders: bool = True

    path: Path | None = field(default=None, repr=False, compare=False)

    # -- io -----------------------------------------------------------------

    @classmethod
    def load(cls, path: Path | None = None) -> ClientConfig:
        path = path or (config_dir() / CONFIG_FILENAME)
        if not path.exists():
            return cls(path=path)
        try:
            with path.open("rb") as handle:
                raw = tomllib.load(handle)
        except (OSError, ValueError):
            # A corrupt config must never stop the game from starting.
            return cls(path=path)

        server_block = raw.get("server", {})
        ui_block = raw.get("ui", {})
        auth_block = raw.get("auth", {})
        return cls(
            server=str(server_block.get("url", DEFAULT_SERVER)),
            tokens={str(k): str(v) for k, v in (auth_block.get("tokens") or {}).items()},
            last_username=str(auth_block.get("last_username", "")),
            chart_style=str(ui_block.get("chart_style", "candles")),
            default_timeframe=str(ui_block.get("default_timeframe", "1D")),
            show_tape=bool(ui_block.get("show_tape", True)),
            confirm_orders=bool(ui_block.get("confirm_orders", True)),
            path=path,
        )

    def save(self) -> None:
        path = self.path or (config_dir() / CONFIG_FILENAME)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload: dict[str, Any] = {
            "server": {"url": self.server},
            "auth": {"tokens": self.tokens, "last_username": self.last_username},
            "ui": {
                "chart_style": self.chart_style,
                "default_timeframe": self.default_timeframe,
                "show_tape": self.show_tape,
                "confirm_orders": self.confirm_orders,
            },
        }
        temporary = path.with_suffix(".toml.tmp")
        with temporary.open("wb") as handle:
            tomli_w.dump(payload, handle)
        os.chmod(temporary, 0o600)
        temporary.replace(path)
        self.path = path

    # -- tokens -------------------------------------------------------------

    def token_for(self, server: str | None = None) -> str | None:
        return self.tokens.get(normalise_server(server or self.server))

    def set_token(self, token: str, server: str | None = None) -> None:
        self.tokens[normalise_server(server or self.server)] = token

    def clear_token(self, server: str | None = None) -> None:
        self.tokens.pop(normalise_server(server or self.server), None)


def normalise_server(value: str) -> str:
    """Accept ``host``, ``host:port`` or a full URL; return a canonical URL.

    ``stocks.example.com`` becomes ``https://stocks.example.com``
    while ``localhost:8765`` stays on http, because nobody runs TLS on their
    development machine.
    """
    value = (value or "").strip().rstrip("/")
    if not value:
        return DEFAULT_SERVER
    if "://" not in value:
        host = value.split("/")[0]
        hostname = host.split(":")[0]
        is_local = hostname in {"localhost", "127.0.0.1", "::1", "0.0.0.0"}
        scheme = "http" if is_local else "https"
        value = f"{scheme}://{value}"
    parsed = urlparse(value)
    port = f":{parsed.port}" if parsed.port else ""
    return f"{parsed.scheme}://{parsed.hostname}{port}"


def websocket_url(server: str) -> str:
    """HTTP(S) server URL -> the matching ws(s) endpoint."""
    base = normalise_server(server)
    if base.startswith("https://"):
        return "wss://" + base[len("https://") :] + "/ws"
    return "ws://" + base[len("http://") :] + "/ws"
