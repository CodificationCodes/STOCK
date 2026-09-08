"""The WebSocket hub: connections, subscriptions and fan-out.

Bandwidth discipline is the point of this module. A naive implementation
broadcasts every price of every symbol to every client several times a
second; with 47 symbols and a 2-second tick that is a lot of JSON for a
player who is staring at one chart.

Instead:

* clients subscribe to named channels and receive **only** those,
* the market channel is sent as a compact ``{s,p,c,v,h,l}`` array,
* the per-symbol channel is only served to clients on that stock's page,
* portfolio pushes are throttled and coalesced -- a burst of fills produces
  one refresh, not one per fill,
* a slow client is dropped rather than allowed to grow an unbounded queue.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from stockgame.shared.protocol import Channel, ServerMessage, encode

if TYPE_CHECKING:  # pragma: no cover
    from fastapi import WebSocket

log = logging.getLogger("stockgame.hub")

#: Frames buffered per connection before it is considered too slow to keep.
SEND_QUEUE_LIMIT = 64
#: Minimum seconds between portfolio pushes to one client.
PORTFOLIO_THROTTLE = 0.75


@dataclass
class Connection:
    """One live client socket."""

    id: int
    socket: WebSocket
    user_id: int
    username: str
    is_admin: bool = False
    channels: set[str] = field(default_factory=set)
    queue: asyncio.Queue[str] = field(default_factory=lambda: asyncio.Queue(SEND_QUEUE_LIMIT))
    connected_at: float = field(default_factory=time.time)
    last_portfolio_push: float = 0.0
    #: Set when the writer task gives up, so the reader stops too.
    closed: asyncio.Event = field(default_factory=asyncio.Event)

    def subscribed(self, key: str) -> bool:
        return key in self.channels

    def send_soon(self, frame: str) -> bool:
        """Queue a frame. Returns False if the client is too far behind."""
        try:
            self.queue.put_nowait(frame)
            return True
        except asyncio.QueueFull:
            return False


class ConnectionHub:
    """Registry of connections plus the fan-out helpers."""

    def __init__(self, max_per_user: int = 5) -> None:
        self._connections: dict[int, Connection] = {}
        self._by_user: dict[int, set[int]] = defaultdict(set)
        self._by_channel: dict[str, set[int]] = defaultdict(set)
        self._next_id = 1
        self.max_per_user = max_per_user

    # -- registry -----------------------------------------------------------

    def add(self, socket: WebSocket, user_id: int, username: str, is_admin: bool) -> Connection:
        connection = Connection(
            id=self._next_id,
            socket=socket,
            user_id=user_id,
            username=username,
            is_admin=is_admin,
        )
        self._next_id += 1
        self._connections[connection.id] = connection
        self._by_user[user_id].add(connection.id)
        return connection

    def remove(self, connection: Connection) -> None:
        self._connections.pop(connection.id, None)
        self._by_user.get(connection.user_id, set()).discard(connection.id)
        if not self._by_user.get(connection.user_id):
            self._by_user.pop(connection.user_id, None)
        for key in list(connection.channels):
            self._by_channel.get(key, set()).discard(connection.id)
            if not self._by_channel.get(key):
                self._by_channel.pop(key, None)
        connection.channels.clear()
        connection.closed.set()

    def connections_for(self, user_id: int) -> list[Connection]:
        return [
            self._connections[cid]
            for cid in list(self._by_user.get(user_id, ()))
            if cid in self._connections
        ]

    def connection_count_for(self, user_id: int) -> int:
        return len(self._by_user.get(user_id, ()))

    def oldest_for(self, user_id: int) -> Connection | None:
        ids = self._by_user.get(user_id)
        if not ids:
            return None
        return min(
            (self._connections[cid] for cid in ids if cid in self._connections),
            key=lambda c: c.connected_at,
            default=None,
        )

    # -- subscriptions ------------------------------------------------------

    def subscribe(self, connection: Connection, key: str) -> None:
        connection.channels.add(key)
        self._by_channel[key].add(connection.id)

    def unsubscribe(self, connection: Connection, key: str) -> None:
        connection.channels.discard(key)
        subscribers = self._by_channel.get(key)
        if subscribers:
            subscribers.discard(connection.id)
            if not subscribers:
                self._by_channel.pop(key, None)

    def subscribers(self, key: str) -> list[Connection]:
        return [
            self._connections[cid]
            for cid in self._by_channel.get(key, ())
            if cid in self._connections
        ]

    def has_subscribers(self, key: str) -> bool:
        return bool(self._by_channel.get(key))

    def active_symbol_channels(self) -> set[str]:
        """Symbols at least one client is currently watching."""
        prefix = f"{Channel.STOCK.value}:"
        return {
            key[len(prefix) :]
            for key in self._by_channel
            if key.startswith(prefix) and self._by_channel[key]
        }

    # -- sending ------------------------------------------------------------

    def send_to_channel(self, key: str, frame: str) -> int:
        sent = 0
        for connection in self.subscribers(key):
            if connection.send_soon(frame):
                sent += 1
            else:
                log.warning(
                    "dropping slow connection user=%s id=%s", connection.username, connection.id
                )
                self._drop(connection)
        return sent

    def send_to_user(self, user_id: int, frame: str) -> int:
        sent = 0
        for cid in list(self._by_user.get(user_id, ())):
            connection = self._connections.get(cid)
            if connection is None:
                continue
            if connection.send_soon(frame):
                sent += 1
            else:
                self._drop(connection)
        return sent

    def broadcast(self, frame: str) -> int:
        sent = 0
        for connection in list(self._connections.values()):
            if connection.send_soon(frame):
                sent += 1
            else:
                self._drop(connection)
        return sent

    def _drop(self, connection: Connection) -> None:
        connection.closed.set()
        self.remove(connection)

    # -- introspection ------------------------------------------------------

    @property
    def total(self) -> int:
        return len(self._connections)

    @property
    def unique_players(self) -> int:
        return len(self._by_user)

    def online_usernames(self) -> list[str]:
        return sorted({c.username for c in self._connections.values()})

    def stats(self) -> dict[str, Any]:
        return {
            "connections": self.total,
            "players_online": self.unique_players,
            "channels": {key: len(ids) for key, ids in self._by_channel.items()},
        }

    async def close_all(self) -> None:
        for connection in list(self._connections.values()):
            connection.closed.set()
            with contextlib.suppress(Exception):
                await connection.socket.close(code=1001)
            self.remove(connection)


def market_frame(ticks: list[dict[str, Any]]) -> str:
    return encode(ServerMessage.PRICE_UPDATE, {"ticks": ticks})
