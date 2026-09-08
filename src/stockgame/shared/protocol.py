"""The client/server wire protocol.

Deliberately dependency-free: the client must be installable without the
server's dependency stack, so this module uses only the standard library.

Envelope
--------
Every frame is a JSON object::

    {"v": 1, "type": "<message type>", "id": "<request id|null>", "data": {...}}

``id`` is set by the client on request frames; the server echoes it back on
the matching reply as ``ref`` so a client can await a specific response while
unsolicited pushes keep flowing on the same socket.
"""

from __future__ import annotations

import json
from decimal import Decimal
from enum import Enum
from typing import Any

from stockgame.shared.enums import StrEnum

#: Bumped whenever a breaking change is made to frame shapes. The server
#: rejects handshakes from clients advertising a different major version.
PROTOCOL_VERSION = 1


class Channel(StrEnum):
    """Subscribable streams.

    Clients receive *only* the channels they subscribe to; this keeps the
    per-connection bandwidth proportional to what is actually on screen.
    """

    #: Compact price ticks for every listed symbol (the market screen + tape).
    MARKET = "market"
    #: Full detail + live candle updates for one symbol. Parameterised:
    #: ``stock:ACME``.
    STOCK = "stock"
    #: The authenticated player's own cash/holdings/orders. Never another's.
    PORTFOLIO = "portfolio"
    #: Global rankings, pushed on recompute.
    LEADERBOARD = "leaderboard"
    #: Simulated financial news headlines.
    NEWS = "news"
    #: Anonymised global trade tape -- what other players are doing.
    TAPE = "tape"
    #: Market status changes: open/closed/halted, regime shifts, day rollover.
    STATUS = "status"

    @staticmethod
    def parse(raw: str) -> tuple[Channel, str | None]:
        """Split ``"stock:ACME"`` into ``(Channel.STOCK, "ACME")``."""
        name, _, param = raw.partition(":")
        return Channel(name), (param or None)

    def key(self, param: str | None = None) -> str:
        return f"{self.value}:{param}" if param else self.value


class ClientMessage(StrEnum):
    """Frame types the client may send."""

    HELLO = "hello"  # handshake + token auth, must be the first frame
    PING = "ping"
    SUBSCRIBE = "subscribe"
    UNSUBSCRIBE = "unsubscribe"
    PLACE_ORDER = "place_order"
    CANCEL_ORDER = "cancel_order"
    GET_CANDLES = "get_candles"
    GET_PORTFOLIO = "get_portfolio"
    GET_ORDERS = "get_orders"
    GET_TRADES = "get_trades"
    GET_LEADERBOARD = "get_leaderboard"
    GET_PROFILE = "get_profile"
    GET_NEWS = "get_news"
    WATCHLIST_ADD = "watchlist_add"
    WATCHLIST_REMOVE = "watchlist_remove"
    GET_WATCHLIST = "get_watchlist"


class ServerMessage(StrEnum):
    """Frame types the server may send."""

    WELCOME = "welcome"  # handshake accepted; carries snapshot metadata
    PONG = "pong"
    ERROR = "error"
    OK = "ok"  # generic acknowledgement for a request with no payload

    # Snapshots (sent once on subscribe) and their incremental updates.
    MARKET_SNAPSHOT = "market_snapshot"
    PRICE_UPDATE = "price_update"
    STOCK_SNAPSHOT = "stock_snapshot"
    CANDLES = "candles"
    PORTFOLIO = "portfolio"
    ORDERS = "orders"
    ORDER_UPDATE = "order_update"
    TRADES = "trades"
    TRADE_EXECUTED = "trade_executed"  # one of *your* fills
    TAPE = "tape"  # someone else's fill (anonymised)
    LEADERBOARD = "leaderboard"
    PROFILE = "profile"
    NEWS = "news"
    NEWS_ITEM = "news_item"
    WATCHLIST = "watchlist"
    MARKET_STATUS = "market_status"


class ErrorCode(StrEnum):
    """Stable, machine-readable failure reasons."""

    BAD_REQUEST = "bad_request"
    UNAUTHENTICATED = "unauthenticated"
    FORBIDDEN = "forbidden"
    NOT_FOUND = "not_found"
    RATE_LIMITED = "rate_limited"
    PROTOCOL_MISMATCH = "protocol_mismatch"
    INSUFFICIENT_FUNDS = "insufficient_funds"
    INSUFFICIENT_SHARES = "insufficient_shares"
    MARKET_CLOSED = "market_closed"
    INVALID_ORDER = "invalid_order"
    INTERNAL = "internal"


class ProtocolError(Exception):
    """Raised when a frame cannot be parsed or fails validation."""

    def __init__(self, message: str, code: ErrorCode = ErrorCode.BAD_REQUEST) -> None:
        super().__init__(message)
        self.code = code


def encode(
    msg_type: str,
    data: dict[str, Any] | None = None,
    *,
    msg_id: str | None = None,
    ref: str | None = None,
) -> str:
    """Serialise a frame to a JSON string."""
    frame: dict[str, Any] = {"v": PROTOCOL_VERSION, "type": str(msg_type)}
    if msg_id is not None:
        frame["id"] = msg_id
    if ref is not None:
        frame["ref"] = ref
    frame["data"] = data if data is not None else {}
    return json.dumps(frame, separators=(",", ":"), default=_json_default)


def decode(raw: str | bytes) -> dict[str, Any]:
    """Parse and shape-check an inbound frame."""
    try:
        frame = json.loads(raw)
    except (ValueError, TypeError) as exc:
        raise ProtocolError(f"malformed JSON: {exc}") from exc
    if not isinstance(frame, dict):
        raise ProtocolError("frame must be a JSON object")
    if not isinstance(frame.get("type"), str):
        raise ProtocolError("frame is missing a string 'type'")
    data = frame.get("data")
    if data is None:
        frame["data"] = {}
    elif not isinstance(data, dict):
        raise ProtocolError("'data' must be a JSON object")
    return frame


def error_frame(code: ErrorCode | str, message: str, *, ref: str | None = None) -> str:
    return encode(ServerMessage.ERROR, {"code": str(code), "message": message}, ref=ref)


def _json_default(value: Any) -> Any:
    """Fallback encoder for enums, datetimes and Decimals."""
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Decimal):
        return float(value)
    if hasattr(value, "isoformat"):
        return value.isoformat()
    raise TypeError(f"object of type {type(value).__name__} is not JSON serialisable")
