"""Server configuration, loaded from the environment (12-factor style).

Nothing here has a production-safe default that involves a secret: if
``STOCKGAME_SECRET_KEY`` is unset the server generates an ephemeral key and
logs a loud warning, which is fine for development and useless for
production (sessions die on restart).
"""

from __future__ import annotations

import secrets
from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="STOCKGAME_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # -- Networking ----------------------------------------------------------
    host: str = "127.0.0.1"
    port: int = 8765
    #: Comma-separated origins allowed to call the HTTP API from a browser.
    #: The terminal client does not need CORS; this exists for future web tools.
    cors_origins: str = ""
    #: Trust ``X-Forwarded-For`` from the reverse proxy when rate limiting.
    trust_proxy_headers: bool = False

    # -- Storage -------------------------------------------------------------
    #: SQLAlchemy URL. Swap for ``postgresql+asyncpg://...`` with no code change.
    database_url: str = "sqlite+aiosqlite:///./data/stockgame.sqlite3"
    data_dir: Path = Path("./data")

    # -- Security ------------------------------------------------------------
    secret_key: str = ""
    session_ttl_hours: int = 24 * 14
    #: Argon2id parameters. Defaults follow the OWASP "second choice" profile.
    argon2_time_cost: int = 3
    argon2_memory_cost: int = 64 * 1024  # KiB
    argon2_parallelism: int = 2
    #: Requests per window for the auth endpoints, per client IP.
    auth_rate_limit: int = 10
    auth_rate_window_seconds: int = 60
    #: Orders per window, per authenticated player.
    order_rate_limit: int = 30
    order_rate_window_seconds: int = 10
    max_connections_per_user: int = 5

    # -- Simulation ----------------------------------------------------------
    starting_cash: float = 100_000.0
    #: Seconds of wall-clock time between price ticks.
    tick_seconds: float = 2.0
    #: Wall-clock length of one simulated trading day. Shorter = faster game.
    day_seconds: int = 3600
    #: Ticks aggregated into one intraday candle.
    ticks_per_candle: int = 15
    #: Days of end-of-day history generated when the market is first seeded.
    history_days: int = 400
    #: Deterministic seed for the initial universe + history. Changing it after
    #: first run has no effect: the market is persisted.
    market_seed: int = 20260101
    #: Per-trade commission in dollars. 0 keeps the game frictionless.
    commission: float = 0.0
    #: Seconds an order sits before it can fill, at the price prevailing then.
    #: This is what stops a player reading a headline and front-running it.
    #: 0 restores instant execution.
    order_delay_seconds: float = 25.0
    #: Master switch. False keeps the market shut regardless of the schedule.
    market_open: bool = True
    #: Wall-clock trading session. Outside it the market freezes: prices stop
    #: moving, the simulated day stops advancing, and orders are rejected.
    market_timezone: str = "Australia/Sydney"
    market_open_time: str = "09:00"
    market_close_time: str = "15:00"
    market_weekdays_only: bool = True

    # -- Leaderboard / seasons ----------------------------------------------
    leaderboard_interval_seconds: int = 30
    leaderboard_size: int = 100
    season_length_days: int = 30

    # -- Operations ----------------------------------------------------------
    log_level: str = "INFO"
    log_json: bool = False
    log_file: Path | None = None
    #: Enables /api/admin/*, which additionally requires an admin user token.
    admin_enabled: bool = True
    debug: bool = False

    #: True when no secret was supplied and an ephemeral one was generated.
    secret_key_is_ephemeral: bool = Field(default=False, exclude=True)

    def model_post_init(self, __context: object) -> None:
        if not self.secret_key:
            object.__setattr__(self, "secret_key", secrets.token_urlsafe(48))
            object.__setattr__(self, "secret_key_is_ephemeral", True)

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")


@lru_cache
def get_settings() -> Settings:
    return Settings()


def reset_settings_cache() -> None:
    """Used by tests that patch the environment between cases."""
    get_settings.cache_clear()
