"""``stockgame-server`` -- run the game server."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from stockgame import __version__


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="stockgame-server",
        description="Run the authoritative Stock Market Game server.",
    )
    parser.add_argument("--host", help="Bind address (default 127.0.0.1)")
    parser.add_argument("--port", type=int, help="Bind port (default 8765)")
    parser.add_argument("--database-url", help="SQLAlchemy URL (sqlite or postgres)")
    parser.add_argument("--log-level", help="DEBUG, INFO, WARNING, ERROR")
    parser.add_argument("--log-file", type=Path, help="Also write logs to this file")
    parser.add_argument("--json-logs", action="store_true", help="Emit structured JSON logs")
    parser.add_argument(
        "--reload", action="store_true", help="Auto-reload on code changes (development only)"
    )
    parser.add_argument("--debug", action="store_true", help="Enable /docs and verbose errors")
    parser.add_argument(
        "--tick-seconds", type=float, help="Seconds between market ticks (default 2.0)"
    )
    parser.add_argument(
        "--day-seconds", type=int, help="Wall-clock length of one simulated trading day"
    )
    parser.add_argument("--version", action="version", version=f"stockgame {__version__}")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    try:
        import uvicorn
    except ImportError:
        print(
            "The server extras are not installed.\n  pip install 'stockgame[server]'",
            file=sys.stderr,
        )
        return 1

    from stockgame.server.config import get_settings, reset_settings_cache
    from stockgame.server.logging_config import configure_logging

    reset_settings_cache()
    settings = get_settings()

    # Command-line flags win over the environment and .env file.
    for attribute, value in (
        ("host", args.host),
        ("port", args.port),
        ("database_url", args.database_url),
        ("log_level", args.log_level),
        ("log_file", args.log_file),
        ("tick_seconds", args.tick_seconds),
        ("day_seconds", args.day_seconds),
    ):
        if value is not None:
            setattr(settings, attribute, value)
    if args.json_logs:
        settings.log_json = True
    if args.debug:
        settings.debug = True

    configure_logging(settings.log_level, json_output=settings.log_json, log_file=settings.log_file)

    banner(settings)

    if args.reload:
        # Reload needs an import string; the app is rebuilt in the child process.
        uvicorn.run(
            "stockgame.server.app:app",
            host=settings.host,
            port=settings.port,
            reload=True,
            log_config=None,
        )
    else:
        from stockgame.server.app import create_app

        uvicorn.run(
            create_app(settings),
            host=settings.host,
            port=settings.port,
            log_config=None,
            # The reverse proxy terminates TLS and sets these; see docs/DEPLOYMENT.md.
            proxy_headers=settings.trust_proxy_headers,
            forwarded_allow_ips="*" if settings.trust_proxy_headers else None,
        )
    return 0


def banner(settings) -> None:
    where = f"http://{settings.host}:{settings.port}"
    print(
        f"""
  ┌───────────────────────────────────────────────┐
  │  STOCK MARKET GAME  ·  server v{__version__:<14}│
  └───────────────────────────────────────────────┘
    API        {where}/api
    WebSocket  ws://{settings.host}:{settings.port}/ws
    Database   {settings.database_url}
    Tick       {settings.tick_seconds}s   Simulated day: {settings.day_seconds}s

    Connect a client with:
      stockgame --server {settings.host}:{settings.port}
""",
    )


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
