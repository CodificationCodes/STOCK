"""``stockgame`` -- the player-facing entry point."""

from __future__ import annotations

import argparse
import asyncio
import sys

from stockgame import __version__
from stockgame.client.config import ClientConfig, config_dir, normalise_server


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="stockgame",
        description="Play the Stock Market Game in your terminal.",
        epilog=f"Configuration is stored in {config_dir()}",
    )
    parser.add_argument(
        "--server",
        "-s",
        help="Server to connect to, e.g. stocks.example.com or 127.0.0.1:8765",
    )
    parser.add_argument(
        "--set-server",
        metavar="ADDRESS",
        help="Save a default server and exit",
    )
    parser.add_argument("--logout", action="store_true", help="End the stored session and exit")
    parser.add_argument(
        "--no-resume",
        action="store_true",
        help="Ignore the stored session and show the login screen",
    )
    parser.add_argument("--config", action="store_true", help="Print the configuration and exit")
    parser.add_argument("--version", action="version", version=f"stockgame {__version__}")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config = ClientConfig.load()

    if args.set_server:
        config.server = normalise_server(args.set_server)
        config.save()
        print(f"Default server set to {config.server}")
        return 0

    if args.logout:
        from stockgame.client.app import logout

        asyncio.run(logout(config))
        print("Signed out.")
        return 0

    if args.config:
        print(f"config file   {config.path or (config_dir() / 'config.toml')}")
        print(f"server        {config.server}")
        print(
            f"signed in     {'yes' if config.token_for() else 'no'}"
            f"{' as ' + config.last_username if config.token_for() else ''}"
        )
        print(f"chart style   {config.chart_style}")
        return 0

    if not sys.stdout.isatty():
        print(
            "stockgame needs an interactive terminal.\n"
            "Run it directly rather than through a pipe or redirect.",
            file=sys.stderr,
        )
        return 1

    from stockgame.client.app import StockGameApp

    app = StockGameApp(config, server=args.server, auto_login=not args.no_resume)
    try:
        app.run()
    except KeyboardInterrupt:  # pragma: no cover
        return 130
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
