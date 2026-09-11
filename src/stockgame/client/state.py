"""Local presentation state.

A plain mirror of what the server has told us. It performs no financial
calculation of its own: every currency figure here arrived pre-computed from
the server, and nothing in the UI derives a balance, a P/L or a fill price.
The only maths done client-side is chart geometry.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Any


@dataclass
class ClientState:
    username: str = ""
    is_admin: bool = False
    connection_status: str = "connecting"
    connection_message: str = "Connecting..."

    #: symbol -> latest market row.
    stocks: dict[str, dict[str, Any]] = field(default_factory=dict)
    sectors: list[dict[str, Any]] = field(default_factory=list)
    market_status: dict[str, Any] = field(default_factory=dict)
    season: dict[str, Any] = field(default_factory=dict)
    players_online: int = 0

    portfolio: dict[str, Any] = field(default_factory=dict)
    watchlist: list[dict[str, Any]] = field(default_factory=list)
    orders: list[dict[str, Any]] = field(default_factory=list)
    trades: list[dict[str, Any]] = field(default_factory=list)
    leaderboard: dict[str, Any] = field(default_factory=dict)
    profile: dict[str, Any] = field(default_factory=dict)
    rank: int | None = None

    news: deque[dict[str, Any]] = field(default_factory=lambda: deque(maxlen=200))
    tape: deque[dict[str, Any]] = field(default_factory=lambda: deque(maxlen=60))
    chat: deque[dict[str, Any]] = field(default_factory=lambda: deque(maxlen=200))

    selected_symbol: str = "ACME"
    timeframe: str = "1D"
    candles: list[dict[str, Any]] = field(default_factory=list)
    stock_detail: dict[str, Any] = field(default_factory=dict)

    # -- updates ------------------------------------------------------------

    def apply_market_snapshot(self, payload: dict[str, Any]) -> None:
        for row in payload.get("stocks", []):
            self.stocks[row["symbol"]] = row
        self.sectors = payload.get("sectors", self.sectors)
        if payload.get("status"):
            self.market_status = payload["status"]

    def recompute_sectors(self) -> None:
        """Re-derive sector heat from the rows we hold.

        The server only sends the sector table with a full snapshot, so
        without this it would sit frozen at whatever it was when the client
        connected while every price underneath it moved.
        """
        buckets: dict[str, list[float]] = {}
        for row in self.stocks.values():
            sector = row.get("sector")
            if sector:
                buckets.setdefault(sector, []).append(row.get("change_pct", 0.0))
        if not buckets:
            return
        self.sectors = sorted(
            (
                {
                    "sector": sector,
                    "change_pct": round(sum(values) / len(values), 3),
                    "count": len(values),
                }
                for sector, values in buckets.items()
            ),
            key=lambda row: row["change_pct"],
            reverse=True,
        )

    def apply_price_update(self, payload: dict[str, Any]) -> None:
        """Merge a compact tick array into the full rows we already hold."""
        for tick in payload.get("ticks", []):
            row = self.stocks.get(tick["s"])
            if row is None:
                continue
            row["price_cents"] = tick["p"]
            row["change_pct"] = tick["c"]
            row["volume"] = tick["v"]
            row["day_high_cents"] = tick["h"]
            row["day_low_cents"] = tick["l"]
            if tick["s"] == self.selected_symbol and self.stock_detail:
                self.stock_detail["price_cents"] = tick["p"]
                self.stock_detail["change_pct"] = tick["c"]
                self.stock_detail["day_high_cents"] = tick["h"]
                self.stock_detail["day_low_cents"] = tick["l"]
                self.stock_detail["volume"] = tick["v"]
                prev = self.stock_detail.get("prev_close_cents") or tick["p"]
                self.stock_detail["change_cents"] = tick["p"] - prev
                if self.candles:
                    last = self.candles[-1]
                    last["c"] = tick["p"]
                    last["h"] = max(last["h"], tick["p"])
                    last["l"] = min(last["l"], tick["p"])
        if payload.get("index") is not None:
            self.market_status["index_value"] = payload["index"]
            self.market_status["index_change_pct"] = payload.get("index_change_pct", 0.0)
        if payload.get("regime"):
            self.market_status["regime"] = payload["regime"]
        self.recompute_sectors()

    def price_of(self, symbol: str) -> int:
        row = self.stocks.get(symbol)
        return int(row["price_cents"]) if row else 0

    def name_of(self, symbol: str) -> str:
        row = self.stocks.get(symbol)
        return str(row["name"]) if row else symbol

    def change_of(self, symbol: str) -> float:
        row = self.stocks.get(symbol)
        return float(row.get("change_pct", 0.0)) if row else 0.0

    def position_in(self, symbol: str) -> dict[str, Any] | None:
        for position in self.portfolio.get("positions", []):
            if position["symbol"] == symbol:
                return position
        return None

    def shares_of(self, symbol: str) -> int:
        position = self.position_in(symbol)
        return int(position["quantity"]) if position else 0

    @property
    def cash_cents(self) -> int:
        return int(self.portfolio.get("cash_cents", 0))

    @property
    def total_value_cents(self) -> int:
        return int(self.portfolio.get("total_value_cents", 0))

    def sorted_stocks(self, key: str = "symbol", reverse: bool = False) -> list[dict[str, Any]]:
        rows = list(self.stocks.values())
        rows.sort(key=lambda row: row.get(key, 0), reverse=reverse)
        return rows

    def watchlist_symbols(self) -> set[str]:
        return {row["symbol"] for row in self.watchlist}

    def open_orders(self) -> list[dict[str, Any]]:
        return [
            order for order in self.orders if order["status"] in ("pending", "partially_filled")
        ]
