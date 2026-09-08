"""Client-side units: config, protocol, formatting and chart rendering.

None of these need a server. They cover the pieces that would silently
corrupt the display if they broke.
"""

from __future__ import annotations

import io
import json

import pytest
from rich.console import Console

from stockgame.client.config import (
    ClientConfig,
    normalise_server,
    websocket_url,
)
from stockgame.client.state import ClientState
from stockgame.client.widgets.chart import (
    Bar,
    PriceChart,
    day_range_marker,
    format_change,
    heat_style,
    scale_bar,
    sparkline,
)
from stockgame.shared.money import fmt_money, fmt_pct, from_cents, pct_change, to_cents
from stockgame.shared.protocol import (
    PROTOCOL_VERSION,
    Channel,
    ClientMessage,
    ProtocolError,
    decode,
    encode,
)


def render(renderable, width: int = 80) -> str:
    buffer = io.StringIO()
    Console(width=width, file=buffer, no_color=True, legacy_windows=False).print(renderable)
    return buffer.getvalue()


class TestServerAddress:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("127.0.0.1:8765", "http://127.0.0.1:8765"),
            ("localhost:8765", "http://localhost:8765"),
            ("stocks.example.com", "https://stocks.example.com"),
            ("https://stocks.example.com/", "https://stocks.example.com"),
            ("http://example.com:9000", "http://example.com:9000"),
            ("", "http://127.0.0.1:8765"),
        ],
    )
    def test_normalisation(self, raw, expected):
        assert normalise_server(raw) == expected

    def test_local_addresses_stay_plaintext_remote_ones_do_not(self):
        # Nobody runs TLS on their laptop; everybody should in production.
        assert normalise_server("127.0.0.1:8765").startswith("http://")
        assert normalise_server("play.example.com").startswith("https://")

    @pytest.mark.parametrize(
        ("server", "expected"),
        [
            ("127.0.0.1:8765", "ws://127.0.0.1:8765/ws"),
            ("stocks.example.com", "wss://stocks.example.com/ws"),
        ],
    )
    def test_websocket_url(self, server, expected):
        assert websocket_url(server) == expected


class TestClientConfig:
    def test_round_trip(self, tmp_path):
        path = tmp_path / "config.toml"
        config = ClientConfig(path=path)
        config.server = "https://play.example.com"
        config.set_token("abc123")
        config.last_username = "alice"
        config.chart_style = "line"
        config.save()

        loaded = ClientConfig.load(path)
        assert loaded.server == "https://play.example.com"
        assert loaded.token_for() == "abc123"
        assert loaded.last_username == "alice"
        assert loaded.chart_style == "line"

    def test_tokens_are_kept_per_server(self, tmp_path):
        config = ClientConfig(path=tmp_path / "config.toml")
        config.set_token("local-token", "127.0.0.1:8765")
        config.set_token("remote-token", "play.example.com")
        config.save()

        loaded = ClientConfig.load(tmp_path / "config.toml")
        assert loaded.token_for("127.0.0.1:8765") == "local-token"
        assert loaded.token_for("play.example.com") == "remote-token"

    def test_clearing_a_token_only_affects_that_server(self, tmp_path):
        config = ClientConfig(path=tmp_path / "config.toml")
        config.set_token("a", "one.example.com")
        config.set_token("b", "two.example.com")
        config.clear_token("one.example.com")
        assert config.token_for("one.example.com") is None
        assert config.token_for("two.example.com") == "b"

    def test_the_file_is_not_world_readable(self, tmp_path):
        path = tmp_path / "config.toml"
        config = ClientConfig(path=path)
        config.set_token("secret")
        config.save()
        assert path.stat().st_mode & 0o077 == 0

    def test_a_corrupt_file_falls_back_to_defaults(self, tmp_path):
        path = tmp_path / "config.toml"
        path.write_text("this is not [valid toml")
        config = ClientConfig.load(path)
        assert config.server == "http://127.0.0.1:8765"

    def test_a_missing_file_is_fine(self, tmp_path):
        assert ClientConfig.load(tmp_path / "nope.toml").server


class TestProtocol:
    def test_encode_decode_round_trip(self):
        frame = decode(encode(ClientMessage.PING, {"t": 1}, msg_id="r1"))
        assert frame["type"] == "ping"
        assert frame["data"] == {"t": 1}
        assert frame["id"] == "r1"
        assert frame["v"] == PROTOCOL_VERSION

    def test_enums_and_datetimes_serialise(self):
        from datetime import datetime, timezone

        payload = json.loads(
            encode(
                "x", {"side": ClientMessage.PING, "at": datetime(2026, 1, 1, tzinfo=timezone.utc)}
            )
        )
        assert payload["data"]["side"] == "ping"
        assert payload["data"]["at"].startswith("2026-01-01")

    @pytest.mark.parametrize(
        "raw",
        [
            "not json",
            "[]",
            '"a string"',
            "123",
            '{"no":"type"}',
            '{"type":1}',
            '{"type":"ping","data":[]}',
        ],
    )
    def test_malformed_frames_are_rejected(self, raw):
        with pytest.raises(ProtocolError):
            decode(raw)

    def test_missing_data_defaults_to_empty(self):
        assert decode('{"type":"ping"}')["data"] == {}

    def test_channel_keys_round_trip(self):
        assert Channel.parse("market") == (Channel.MARKET, None)
        assert Channel.parse("stock:ACME") == (Channel.STOCK, "ACME")
        assert Channel.STOCK.key("ACME") == "stock:ACME"
        assert Channel.MARKET.key() == "market"


class TestMoneyFormatting:
    @pytest.mark.parametrize(
        ("cents", "expected"),
        [
            (0, "$0.00"),
            (100, "$1.00"),
            (1_234_567, "$12,345.67"),
            (-4250, "-$42.50"),
            (1, "$0.01"),
        ],
    )
    def test_fmt_money(self, cents, expected):
        assert fmt_money(cents) == expected

    def test_signed_and_compact(self):
        assert fmt_money(1234, sign=True) == "+$12.34"
        assert fmt_money(-1234, sign=True) == "-$12.34"
        assert fmt_money(0, sign=True) == "$0.00"
        assert fmt_money(9_876_500_000, compact=True) == "$98.77M"
        assert fmt_money(500_000_000_000, compact=True) == "$5.00B"

    def test_percentages(self):
        assert fmt_pct(2.4) == "+2.40%"
        assert fmt_pct(-1.5) == "-1.50%"
        assert fmt_pct(0) == "0.00%"

    @pytest.mark.parametrize(
        ("value", "cents"), [(1.005, 101), (0.1, 10), ("142.37", 14237), (2.675, 268)]
    )
    def test_to_cents_rounds_half_up(self, value, cents):
        assert to_cents(value) == cents

    def test_cents_round_trip_without_drift(self):
        for cents in (1, 99, 100, 10_000_000, 123_456_789):
            assert to_cents(from_cents(cents)) == cents

    def test_pct_change(self):
        assert pct_change(110, 100) == pytest.approx(10.0)
        assert pct_change(90, 100) == pytest.approx(-10.0)
        assert pct_change(100, 0) == 0.0


class TestChartRendering:
    def make_bars(self, count: int = 40) -> list[Bar]:
        import random

        rng = random.Random(3)
        bars, price = [], 10_000
        for _ in range(count):
            open_price = price
            price = max(100, int(price * (1 + rng.gauss(0, 0.02))))
            bars.append(
                Bar(
                    open_price,
                    max(open_price, price) + 40,
                    min(open_price, price) - 40,
                    price,
                    rng.randint(10, 500),
                )
            )
        return bars

    def test_candles_render_within_the_given_box(self):
        output = render(PriceChart(self.make_bars(), height=16), width=90)
        lines = output.splitlines()
        assert len(lines) <= 18
        assert all(len(line) <= 90 for line in lines)
        assert any("█" in line or "▀" in line or "▄" in line for line in lines)

    def test_line_mode_uses_braille(self):
        output = render(PriceChart(self.make_bars(), style="line", height=14), width=90)
        assert any(0x2800 <= ord(ch) <= 0x28FF for ch in output)

    def test_price_axis_is_labelled(self):
        assert "$" in render(PriceChart(self.make_bars(), height=14), width=80)

    def test_volume_strip_is_drawn_when_there_is_room(self):
        assert "VOL" in render(PriceChart(self.make_bars(), height=16), width=80)

    def test_no_volume_strip_in_a_short_box(self):
        assert "VOL" not in render(PriceChart(self.make_bars(), height=8), width=80)

    def test_empty_data_does_not_crash(self):
        assert "no price history" in render(PriceChart([]), width=60)

    def test_a_single_bar_renders(self):
        render(PriceChart([Bar(100, 110, 90, 105, 1)], height=10), width=60)

    def test_a_flat_series_does_not_divide_by_zero(self):
        flat = [Bar(1000, 1000, 1000, 1000, 5) for _ in range(20)]
        render(PriceChart(flat, height=12), width=60)

    def test_few_bars_fill_the_available_width(self):
        """A 3M chart on a wide terminal should not leave two thirds blank."""
        narrow = render(PriceChart(self.make_bars(10), height=12), width=100)
        painted = max(
            len(line.rstrip()) - len(line) + len(line.lstrip()) for line in narrow.splitlines()
        )
        assert painted > 30

    def test_the_reference_line_is_drawn(self):
        output = render(PriceChart(self.make_bars(), reference=10_000, height=14), width=80)
        assert "╌" in output

    def test_charts_survive_extreme_values(self):
        bars = [Bar(1, 2, 1, 2, 0), Bar(2, 10_000_000, 1, 9_999_999, 10**9)]
        render(PriceChart(bars, height=12), width=60)


class TestSmallWidgets:
    def test_sparkline_is_the_requested_width(self):
        assert len(sparkline([1, 5, 2, 8, 3], 12).plain) == 12

    def test_sparkline_handles_empty_and_flat_input(self):
        assert len(sparkline([], 8).plain) == 8
        assert len(sparkline([5, 5, 5], 6).plain) == 6

    def test_change_formatting_picks_a_direction(self):
        assert "▲" in format_change(2.4).plain
        assert "▼" in format_change(-2.4).plain
        assert "•" in format_change(0.0).plain

    def test_heat_style_is_monotonic(self):
        assert heat_style(5) != heat_style(-5)
        assert heat_style(0) == heat_style(0)

    @pytest.mark.parametrize("fraction", [0.0, 0.25, 0.5, 1.0, 1.5, -0.2])
    def test_scale_bar_is_always_the_requested_width(self, fraction):
        assert len(scale_bar(fraction, 10)) == 10

    def test_day_range_marker_places_the_dot(self):
        assert "●" in day_range_marker(100, 200, 150, 16).plain
        low = day_range_marker(100, 200, 100, 16).plain
        high = day_range_marker(100, 200, 200, 16).plain
        assert low.index("●") < high.index("●")

    def test_day_range_marker_with_no_range(self):
        day_range_marker(100, 100, 100, 12)


class TestClientState:
    def make_state(self) -> ClientState:
        state = ClientState()
        state.stocks = {
            "ACME": {
                "symbol": "ACME",
                "name": "Acme Technologies",
                "sector": "Technology",
                "price_cents": 10_000,
                "change_pct": 0.0,
                "volume": 0,
                "day_high_cents": 10_000,
                "day_low_cents": 10_000,
            }
        }
        return state

    def test_price_updates_merge_into_existing_rows(self):
        state = self.make_state()
        state.apply_price_update(
            {"ticks": [{"s": "ACME", "p": 11_000, "c": 10.0, "v": 500, "h": 11_100, "l": 9_900}]}
        )
        row = state.stocks["ACME"]
        assert row["price_cents"] == 11_000
        assert row["change_pct"] == 10.0
        # The fields the tick does not carry are preserved.
        assert row["name"] == "Acme Technologies"

    def test_ticks_for_unknown_symbols_are_ignored(self):
        state = self.make_state()
        state.apply_price_update({"ticks": [{"s": "ZZZZ", "p": 1, "c": 0, "v": 0, "h": 1, "l": 1}]})
        assert "ZZZZ" not in state.stocks

    def test_the_selected_symbol_detail_follows_the_tick(self):
        state = self.make_state()
        state.selected_symbol = "ACME"
        state.stock_detail = {"symbol": "ACME", "price_cents": 10_000, "prev_close_cents": 9_000}
        state.candles = [{"o": 9_000, "h": 10_000, "l": 8_900, "c": 10_000, "v": 1}]
        state.apply_price_update(
            {"ticks": [{"s": "ACME", "p": 12_000, "c": 33.3, "v": 9, "h": 12_000, "l": 8_900}]}
        )
        assert state.stock_detail["price_cents"] == 12_000
        assert state.stock_detail["change_cents"] == 3_000
        # The live candle's right edge tracks the price.
        assert state.candles[-1]["c"] == 12_000
        assert state.candles[-1]["h"] == 12_000

    def test_position_helpers(self):
        state = self.make_state()
        state.portfolio = {
            "cash_cents": 5_000,
            "total_value_cents": 15_000,
            "positions": [{"symbol": "ACME", "quantity": 100}],
        }
        assert state.shares_of("ACME") == 100
        assert state.shares_of("NOVA") == 0
        assert state.position_in("NOVA") is None
        assert state.cash_cents == 5_000

    def test_open_orders_filters_terminal_states(self):
        state = ClientState()
        state.orders = [
            {"id": "1", "status": "pending"},
            {"id": "2", "status": "filled"},
            {"id": "3", "status": "partially_filled"},
            {"id": "4", "status": "cancelled"},
        ]
        assert {o["id"] for o in state.open_orders()} == {"1", "3"}

    def test_index_updates_are_applied(self):
        state = ClientState()
        state.apply_price_update(
            {"ticks": [], "index": 101_000, "index_change_pct": 1.0, "regime": "bull"}
        )
        assert state.market_status["index_value"] == 101_000
        assert state.market_status["regime"] == "bull"


class TestCLI:
    def test_help_does_not_crash(self):
        from stockgame.client.cli import build_parser

        assert build_parser().format_help()

    def test_set_server_writes_the_config(self, tmp_path, monkeypatch):
        monkeypatch.setenv("STOCKGAME_CONFIG_DIR", str(tmp_path))
        from stockgame.client.cli import main

        assert main(["--set-server", "play.example.com"]) == 0
        assert ClientConfig.load(tmp_path / "config.toml").server == "https://play.example.com"

    def test_config_subcommand(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv("STOCKGAME_CONFIG_DIR", str(tmp_path))
        from stockgame.client.cli import main

        assert main(["--config"]) == 0
        assert "server" in capsys.readouterr().out

    def test_server_cli_parser(self):
        from stockgame.server.cli import build_parser

        args = build_parser().parse_args(["--host", "0.0.0.0", "--port", "9000"])
        assert args.host == "0.0.0.0"
        assert args.port == 9000

    def test_admin_cli_parser(self):
        from stockgame.server.admin import build_parser

        assert build_parser().parse_args(["player", "alice"]).username == "alice"
        assert build_parser().parse_args(["trades", "--limit", "5"]).limit == 5
