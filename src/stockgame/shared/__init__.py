"""Code shared by the client and the server.

Nothing in here may import from :mod:`stockgame.server` or
:mod:`stockgame.client`. It holds the wire protocol, enums and pure helper
functions so both sides agree on names and shapes without the client ever
needing the server's dependency stack.
"""

from stockgame.shared.enums import (
    MarketRegime,
    NewsScope,
    OrderSide,
    OrderStatus,
    OrderType,
    Sector,
)
from stockgame.shared.protocol import (
    PROTOCOL_VERSION,
    Channel,
    ClientMessage,
    ServerMessage,
)

__all__ = [
    "PROTOCOL_VERSION",
    "Channel",
    "ClientMessage",
    "MarketRegime",
    "NewsScope",
    "OrderSide",
    "OrderStatus",
    "OrderType",
    "Sector",
    "ServerMessage",
]
