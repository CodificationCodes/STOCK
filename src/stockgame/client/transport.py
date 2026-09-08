"""Talking to the server: REST for auth, WebSocket for everything else.

:class:`GameConnection` multiplexes a single socket. A request gets an id and
returns a future that resolves when the matching ``ref`` comes back, while
unsolicited pushes are routed to registered handlers -- so the UI can
``await connection.request(...)`` without blocking the live price stream.

It also reconnects on its own. Losing a connection is a network event, not a
game event: the player's portfolio is on the server and is unaffected.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import httpx
import websockets

from stockgame import __version__
from stockgame.client.config import normalise_server, websocket_url
from stockgame.shared.protocol import ClientMessage, ServerMessage, decode, encode

log = logging.getLogger("stockgame.client")

REQUEST_TIMEOUT = 15.0
RECONNECT_BASE_DELAY = 1.0
RECONNECT_MAX_DELAY = 30.0
HTTP_TIMEOUT = 20.0


class ClientError(Exception):
    """A failure worth showing the player."""

    def __init__(self, message: str, code: str = "error") -> None:
        super().__init__(message)
        self.message = message
        self.code = code


@dataclass(slots=True)
class Account:
    token: str
    username: str
    is_admin: bool = False
    is_new: bool = False
    starting_cash_cents: int = 0


class AuthClient:
    """REST calls made before the socket exists."""

    def __init__(self, server: str) -> None:
        self.server = normalise_server(server)

    async def info(self) -> dict[str, Any]:
        return await self._request("GET", "/api/info")

    async def register(self, username: str, password: str) -> Account:
        data = await self._request(
            "POST", "/api/auth/register", json={"username": username, "password": password}
        )
        return _account(data, is_new=True)

    async def login(self, username: str, password: str) -> Account:
        data = await self._request(
            "POST", "/api/auth/login", json={"username": username, "password": password}
        )
        return _account(data)

    async def logout(self, token: str) -> None:
        with contextlib.suppress(ClientError):
            await self._request(
                "POST", "/api/auth/logout", headers={"Authorization": f"Bearer {token}"}
            )

    async def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        try:
            async with httpx.AsyncClient(
                base_url=self.server,
                timeout=HTTP_TIMEOUT,
                headers={"User-Agent": f"stockgame/{__version__}"},
            ) as client:
                response = await client.request(method, path, **kwargs)
        except httpx.ConnectError as exc:
            raise ClientError(
                f"Could not reach {self.server}. Is the server running?", "unreachable"
            ) from exc
        except httpx.HTTPError as exc:
            raise ClientError(f"Network error: {exc}", "network") from exc

        if response.status_code == 204:
            return {}
        try:
            payload = response.json()
        except ValueError:
            payload = {}
        if response.is_success:
            return payload
        raise ClientError(_error_message(payload, response.status_code), _error_code(payload))


def _account(data: dict[str, Any], *, is_new: bool = False) -> Account:
    return Account(
        token=data["token"],
        username=data["username"],
        is_admin=bool(data.get("is_admin")),
        is_new=bool(data.get("is_new_account", is_new)),
        starting_cash_cents=int(data.get("starting_cash_cents", 0)),
    )


def _error_message(payload: dict[str, Any], status: int) -> str:
    detail = payload.get("detail")
    if isinstance(detail, str):
        return detail
    if isinstance(detail, list) and detail:
        # FastAPI validation errors.
        first = detail[0]
        if isinstance(first, dict):
            field = ".".join(str(part) for part in first.get("loc", [])[1:])
            return f"{field}: {first.get('msg', 'invalid value')}".strip(": ")
    return {
        401: "Incorrect username or password.",
        403: "This account cannot sign in.",
        409: "That username is already taken.",
        429: "Too many attempts. Please wait a moment.",
    }.get(status, f"Server returned {status}.")


def _error_code(payload: dict[str, Any]) -> str:
    code = payload.get("code")
    return str(code) if isinstance(code, str) else "error"


class GameConnection:
    """A reconnecting, multiplexed WebSocket session."""

    def __init__(self, server: str, token: str) -> None:
        self.url = websocket_url(server)
        self.token = token
        self.connected = asyncio.Event()
        self.welcome: dict[str, Any] = {}

        self._socket: Any = None
        self._pending: dict[str, asyncio.Future] = {}
        self._handlers: dict[str, list[Callable[[dict[str, Any]], Awaitable[None] | None]]] = {}
        self._status_handlers: list[Callable[[str, str], Awaitable[None] | None]] = []
        self._subscriptions: set[str] = set()
        self._task: asyncio.Task | None = None
        self._counter = 0
        self._closing = False
        self._attempt = 0

    # -- lifecycle ----------------------------------------------------------

    async def start(self) -> None:
        self._closing = False
        self._task = asyncio.create_task(self._run(), name="ws-client")

    async def close(self) -> None:
        self._closing = True
        if self._socket is not None:
            with contextlib.suppress(Exception):
                await self._socket.close()
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
        self._fail_pending(ClientError("Disconnected.", "disconnected"))

    async def _run(self) -> None:
        while not self._closing:
            try:
                await self._session()
                self._attempt = 0
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if self._closing:
                    return
                log.debug("connection failed: %s", exc)
                await self._notify_status("error", str(exc))

            if self._closing:
                return
            self.connected.clear()
            self._fail_pending(ClientError("Connection lost.", "disconnected"))
            self._attempt += 1
            # Exponential backoff with jitter, so a server restart does not
            # bring every client back in the same millisecond.
            delay = min(RECONNECT_MAX_DELAY, RECONNECT_BASE_DELAY * 2 ** (self._attempt - 1))
            delay *= 0.7 + random.random() * 0.6
            await self._notify_status("reconnecting", f"Reconnecting in {delay:.0f}s...")
            await asyncio.sleep(delay)

    async def _session(self) -> None:
        await self._notify_status("connecting", "Connecting...")
        async with websockets.connect(
            self.url,
            max_size=4 * 1024 * 1024,
            ping_interval=20,
            ping_timeout=20,
            open_timeout=15,
        ) as socket:
            self._socket = socket
            await socket.send(
                encode(
                    ClientMessage.HELLO,
                    {"token": self.token, "client": f"stockgame-tui/{__version__}"},
                )
            )
            raw = await asyncio.wait_for(socket.recv(), timeout=REQUEST_TIMEOUT)
            frame = decode(raw)
            if frame["type"] == ServerMessage.ERROR.value:
                data = frame["data"]
                raise ClientError(data.get("message", "Rejected."), data.get("code", "error"))
            if frame["type"] != ServerMessage.WELCOME.value:
                raise ClientError("Unexpected handshake response.", "protocol")

            self.welcome = frame["data"]
            self.connected.set()
            await self._notify_status("connected", "Connected")

            # Restore whatever the UI had subscribed to before the drop.
            if self._subscriptions:
                await self.send(ClientMessage.SUBSCRIBE, {"channels": sorted(self._subscriptions)})

            async for message in socket:
                await self._dispatch(message)
        self._socket = None

    # -- messaging ----------------------------------------------------------

    async def send(self, kind: ClientMessage | str, data: dict[str, Any] | None = None) -> None:
        socket = self._socket
        if socket is None:
            raise ClientError("Not connected.", "disconnected")
        await socket.send(encode(kind, data or {}))

    async def request(
        self,
        kind: ClientMessage | str,
        data: dict[str, Any] | None = None,
        timeout: float = REQUEST_TIMEOUT,
    ) -> dict[str, Any]:
        """Send a frame and await the reply that carries the matching ``ref``."""
        socket = self._socket
        if socket is None:
            raise ClientError("Not connected.", "disconnected")
        self._counter += 1
        request_id = f"r{self._counter}"
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        try:
            await socket.send(encode(kind, data or {}, msg_id=request_id))
            return await asyncio.wait_for(future, timeout=timeout)
        except (TimeoutError, asyncio.TimeoutError) as exc:
            raise ClientError("The server did not respond in time.", "timeout") from exc
        finally:
            self._pending.pop(request_id, None)

    async def subscribe(self, *channels: str) -> None:
        self._subscriptions.update(channels)
        if self._socket is not None:
            await self.send(ClientMessage.SUBSCRIBE, {"channels": list(channels)})

    async def unsubscribe(self, *channels: str) -> None:
        self._subscriptions.difference_update(channels)
        if self._socket is not None:
            await self.send(ClientMessage.UNSUBSCRIBE, {"channels": list(channels)})

    async def watch_symbol(self, symbol: str) -> None:
        """Swap the per-symbol subscription to a new stock."""
        stale = {c for c in self._subscriptions if c.startswith("stock:")}
        self._subscriptions.difference_update(stale)
        await self.subscribe(f"stock:{symbol}")

    def on(self, kind: ServerMessage | str, handler) -> None:
        self._handlers.setdefault(str(kind), []).append(handler)

    def on_status(self, handler) -> None:
        self._status_handlers.append(handler)

    async def _dispatch(self, raw: str | bytes) -> None:
        try:
            frame = decode(raw)
        except Exception:
            log.debug("dropped malformed frame")
            return

        ref = frame.get("ref")
        if ref is not None:
            future = self._pending.pop(ref, None)
            if future is not None and not future.done():
                if frame["type"] == ServerMessage.ERROR.value:
                    data = frame["data"]
                    future.set_exception(
                        ClientError(
                            data.get("message", "Request failed."), data.get("code", "error")
                        )
                    )
                else:
                    future.set_result(frame["data"])
                return

        if frame["type"] == ServerMessage.ERROR.value:
            data = frame["data"]
            await self._notify_status("error", data.get("message", "Server error."))

        for handler in self._handlers.get(frame["type"], ()):
            try:
                result = handler(frame["data"])
                if asyncio.iscoroutine(result):
                    await result
            except Exception:
                log.exception("client handler for %s failed", frame["type"])

    async def _notify_status(self, state: str, message: str) -> None:
        for handler in self._status_handlers:
            try:
                result = handler(state, message)
                if asyncio.iscoroutine(result):
                    await result
            except Exception:
                log.exception("status handler failed")

    def _fail_pending(self, error: Exception) -> None:
        for future in self._pending.values():
            if not future.done():
                future.set_exception(error)
        self._pending.clear()


def parse_ws_error(raw: str) -> str:  # pragma: no cover - convenience
    with contextlib.suppress(Exception):
        return json.loads(raw)["data"]["message"]
    return "Connection error."
