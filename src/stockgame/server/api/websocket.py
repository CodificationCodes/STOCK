"""The WebSocket gateway.

Connection lifecycle:

1. Client connects and must send a ``hello`` frame carrying its session token
   within :data:`HANDSHAKE_TIMEOUT` seconds. Tokens are *not* accepted in the
   query string, where they would end up in proxy access logs.
2. On success the server replies ``welcome`` and starts a writer task that
   drains that connection's queue.
3. The reader loop dispatches request frames. Every handler re-derives the
   acting user from the authenticated connection -- a frame claiming to be
   from someone else is simply ignored, because the ``user_id`` in the frame
   is never read.

Reads are strictly bounded: oversized frames are rejected before parsing and
order requests pass through the shared rate limiter.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any

from fastapi import WebSocket, WebSocketDisconnect

from stockgame.server.api.hub import PORTFOLIO_THROTTLE, Connection, ConnectionHub
from stockgame.server.core.errors import GameError
from stockgame.server.core.events import Event, Topics
from stockgame.server.core.game import GameServer
from stockgame.server.services.trading import OrderRequest
from stockgame.shared.protocol import (
    PROTOCOL_VERSION,
    Channel,
    ClientMessage,
    ErrorCode,
    ProtocolError,
    ServerMessage,
    decode,
    encode,
)
from stockgame.shared.validation import ValidationError

log = logging.getLogger("stockgame.ws")

HANDSHAKE_TIMEOUT = 10.0
MAX_FRAME_BYTES = 8 * 1024
#: Channels a client is allowed to name in a subscribe frame.
SUBSCRIBABLE = {
    Channel.MARKET,
    Channel.STOCK,
    Channel.PORTFOLIO,
    Channel.LEADERBOARD,
    Channel.NEWS,
    Channel.TAPE,
    Channel.STATUS,
}


class WebSocketGateway:
    def __init__(self, game: GameServer) -> None:
        self.game = game
        self.hub = ConnectionHub(max_per_user=game.settings.max_connections_per_user)
        game.tick_listeners.append(self._on_tick)
        game.bus.subscribe(Topics.ORDER_UPDATED, self._on_order_updated)
        game.bus.subscribe(Topics.TRADE_EXECUTED, self._on_trade_executed)
        game.bus.subscribe(Topics.PORTFOLIO_CHANGED, self._on_portfolio_changed)
        game.bus.subscribe(Topics.TAPE_PRINTED, self._on_tape)
        game.bus.subscribe(Topics.NEWS_PUBLISHED, self._on_news)
        game.bus.subscribe(Topics.LEADERBOARD_UPDATED, self._on_leaderboard)
        game.bus.subscribe(Topics.MARKET_STATUS, self._on_market_status)
        game.bus.subscribe(Topics.ACHIEVEMENT_UNLOCKED, self._on_achievement)

    # -- connection ---------------------------------------------------------

    async def handle(self, socket: WebSocket) -> None:
        await socket.accept()
        connection: Connection | None = None
        writer: asyncio.Task | None = None
        try:
            identity = await self._handshake(socket)
            if identity is None:
                return
            user, client_info = identity

            connection = self.hub.add(socket, user.id, user.username, user.is_admin)
            # Cap concurrent sockets per account: cheap protection against a
            # script opening thousands of connections on one login.
            while self.hub.connection_count_for(user.id) > self.hub.max_per_user:
                oldest = self.hub.oldest_for(user.id)
                if oldest is None or oldest.id == connection.id:
                    break
                log.info("evicting oldest connection for %s", user.username)
                oldest.closed.set()
                with contextlib.suppress(Exception):
                    await oldest.socket.close(code=1008)
                self.hub.remove(oldest)

            writer = asyncio.create_task(
                self._writer(connection), name=f"ws-writer-{connection.id}"
            )
            await socket.send_text(
                encode(
                    ServerMessage.WELCOME,
                    {
                        "protocol": PROTOCOL_VERSION,
                        "username": user.username,
                        "is_admin": user.is_admin,
                        "market": self.game.engine.market_status(),
                        "season": self.game.leaderboard.season_info(),
                        "players_online": self.hub.unique_players,
                        "starting_cash_cents": int(self.game.settings.starting_cash * 100),
                    },
                )
            )
            log.info(
                "ws connect user=%s conns=%d client=%r",
                user.username,
                self.hub.total,
                client_info,
            )
            self._announce_presence()
            await self._reader(connection)
        except WebSocketDisconnect:
            pass
        except Exception:
            log.exception("websocket handler crashed")
        finally:
            if writer is not None:
                writer.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await writer
            if connection is not None:
                self.hub.remove(connection)
                log.info("ws disconnect user=%s conns=%d", connection.username, self.hub.total)
                self._announce_presence()

    async def _handshake(self, socket: WebSocket):
        try:
            raw = await asyncio.wait_for(socket.receive_text(), timeout=HANDSHAKE_TIMEOUT)
        except (TimeoutError, asyncio.TimeoutError):
            await self._close_with(socket, ErrorCode.UNAUTHENTICATED, "Handshake timed out.")
            return None
        except (WebSocketDisconnect, RuntimeError):
            return None

        try:
            frame = decode(raw)
        except ProtocolError as exc:
            await self._close_with(socket, exc.code, str(exc))
            return None

        if frame["type"] != str(ClientMessage.HELLO.value):
            await self._close_with(
                socket, ErrorCode.UNAUTHENTICATED, "Expected a hello frame first."
            )
            return None
        if frame.get("v") != PROTOCOL_VERSION:
            await self._close_with(
                socket,
                ErrorCode.PROTOCOL_MISMATCH,
                f"Server speaks protocol v{PROTOCOL_VERSION}. Please update your client.",
            )
            return None

        token = frame["data"].get("token")
        try:
            user = await self.game.auth.authenticate(str(token or ""))
        except GameError as exc:
            await self._close_with(socket, exc.code, exc.message)
            return None
        return user, str(frame["data"].get("client", ""))[:80]

    async def _close_with(self, socket: WebSocket, code: ErrorCode, message: str) -> None:
        with contextlib.suppress(Exception):
            await socket.send_text(
                encode(ServerMessage.ERROR, {"code": str(code), "message": message})
            )
            await socket.close(code=1008)

    async def _writer(self, connection: Connection) -> None:
        """Drain one connection's queue. Isolated per socket so a stalled
        client cannot block the tick loop."""
        try:
            while not connection.closed.is_set():
                frame = await connection.queue.get()
                await connection.socket.send_text(frame)
        except asyncio.CancelledError:
            raise
        except Exception:
            connection.closed.set()

    async def _reader(self, connection: Connection) -> None:
        while not connection.closed.is_set():
            raw = await connection.socket.receive_text()
            if len(raw) > MAX_FRAME_BYTES:
                await self._error(connection, ErrorCode.BAD_REQUEST, "Frame too large.")
                continue
            try:
                frame = decode(raw)
            except ProtocolError as exc:
                await self._error(connection, exc.code, str(exc))
                continue
            try:
                await self._dispatch(connection, frame)
            except GameError as exc:
                await self._error(connection, exc.code, exc.message, ref=frame.get("id"))
            except ValidationError as exc:
                await self._error(connection, ErrorCode.BAD_REQUEST, str(exc), ref=frame.get("id"))
            except Exception:
                log.exception("error handling %s", frame.get("type"))
                await self._error(
                    connection, ErrorCode.INTERNAL, "Something went wrong.", ref=frame.get("id")
                )

    async def _error(
        self, connection: Connection, code: ErrorCode | str, message: str, ref: str | None = None
    ) -> None:
        connection.send_soon(
            encode(ServerMessage.ERROR, {"code": str(code), "message": message}, ref=ref)
        )

    # -- request dispatch ---------------------------------------------------

    async def _dispatch(self, connection: Connection, frame: dict[str, Any]) -> None:
        kind = frame["type"]
        data = frame["data"]
        ref = frame.get("id")

        if kind == ClientMessage.PING.value:
            connection.send_soon(encode(ServerMessage.PONG, {"t": data.get("t")}, ref=ref))
            return

        if kind == ClientMessage.SUBSCRIBE.value:
            await self._subscribe(connection, data.get("channels") or [], ref)
            return

        if kind == ClientMessage.UNSUBSCRIBE.value:
            for raw_channel in (data.get("channels") or [])[:32]:
                with contextlib.suppress(ValueError):
                    channel, param = Channel.parse(str(raw_channel))
                    self.hub.unsubscribe(connection, channel.key(param))
            connection.send_soon(encode(ServerMessage.OK, {}, ref=ref))
            return

        handler = _HANDLERS.get(kind)
        if handler is None:
            await self._error(connection, ErrorCode.BAD_REQUEST, f"Unknown message: {kind}", ref)
            return
        await handler(self, connection, data, ref)

    async def _subscribe(
        self, connection: Connection, channels: list[Any], ref: str | None
    ) -> None:
        for raw_channel in channels[:32]:
            try:
                channel, param = Channel.parse(str(raw_channel))
            except ValueError:
                continue
            if channel not in SUBSCRIBABLE:
                continue
            if channel is Channel.STOCK:
                if not param or param.upper() not in self.game.engine.sims:
                    continue
                param = param.upper()
                # Watching one stock at a time keeps per-client traffic flat.
                for existing in list(connection.channels):
                    if existing.startswith(f"{Channel.STOCK.value}:"):
                        self.hub.unsubscribe(connection, existing)
            self.hub.subscribe(connection, channel.key(param))
            await self._send_snapshot(connection, channel, param, ref)
        connection.send_soon(
            encode(ServerMessage.OK, {"channels": sorted(connection.channels)}, ref=ref)
        )

    async def _send_snapshot(
        self, connection: Connection, channel: Channel, param: str | None, ref: str | None
    ) -> None:
        """Immediately give a new subscriber the current state of the channel."""
        if channel is Channel.MARKET:
            connection.send_soon(
                encode(
                    ServerMessage.MARKET_SNAPSHOT,
                    {
                        "stocks": self.game.engine.snapshot(),
                        "sectors": self.game.engine.sector_performance(),
                        "status": self.game.engine.market_status(),
                    },
                    ref=ref,
                )
            )
        elif channel is Channel.STOCK and param:
            detail = await self.game.portfolios.stock_detail(param)
            connection.send_soon(encode(ServerMessage.STOCK_SNAPSHOT, detail, ref=ref))
        elif channel is Channel.PORTFOLIO:
            await self._push_portfolio(connection, force=True)
        elif channel is Channel.LEADERBOARD:
            payload = self.game.leaderboard.payload()
            payload["your_rank"] = self.game.leaderboard.rank_of(connection.user_id)
            connection.send_soon(encode(ServerMessage.LEADERBOARD, payload, ref=ref))
        elif channel is Channel.NEWS:
            items = await self.game.market_data.news(limit=40)
            connection.send_soon(encode(ServerMessage.NEWS, {"items": items}, ref=ref))
        elif channel is Channel.TAPE:
            prints = await self.game.market_data.recent_tape(25)
            for item in prints:
                connection.send_soon(encode(ServerMessage.TAPE, item))
        elif channel is Channel.STATUS:
            connection.send_soon(
                encode(
                    ServerMessage.MARKET_STATUS,
                    {
                        **self.game.engine.market_status(),
                        "players_online": self.hub.unique_players,
                    },
                    ref=ref,
                )
            )

    # -- handlers -----------------------------------------------------------

    async def _handle_place_order(
        self, connection: Connection, data: dict[str, Any], ref: str | None
    ) -> None:
        limit = self.game.order_limiter.check(f"user:{connection.user_id}")
        if not limit.allowed:
            await self._error(
                connection,
                ErrorCode.RATE_LIMITED,
                f"Too many orders. Try again in {limit.retry_after:.0f}s.",
                ref,
            )
            return
        request = OrderRequest.parse(data)
        result = await self.game.trading.place_order(connection.user_id, request)
        connection.send_soon(encode(ServerMessage.ORDER_UPDATE, result, ref=ref))
        await self._push_portfolio(connection, force=True)

    async def _handle_cancel_order(
        self, connection: Connection, data: dict[str, Any], ref: str | None
    ) -> None:
        order = await self.game.trading.cancel_order(
            connection.user_id, str(data.get("order_id", ""))[:64]
        )
        connection.send_soon(encode(ServerMessage.ORDER_UPDATE, {"order": order}, ref=ref))
        await self._push_portfolio(connection, force=True)

    async def _handle_get_candles(
        self, connection: Connection, data: dict[str, Any], ref: str | None
    ) -> None:
        payload = await self.game.market_data.candles(
            str(data.get("symbol", "")),
            str(data.get("timeframe", "1D")),
            int(data.get("points") or 120),
        )
        connection.send_soon(encode(ServerMessage.CANDLES, payload, ref=ref))

    async def _handle_get_portfolio(
        self, connection: Connection, data: dict[str, Any], ref: str | None
    ) -> None:
        payload = await self.game.portfolios.get_portfolio(connection.user_id)
        connection.send_soon(encode(ServerMessage.PORTFOLIO, payload, ref=ref))

    async def _handle_get_orders(
        self, connection: Connection, data: dict[str, Any], ref: str | None
    ) -> None:
        orders = await self.game.trading.list_orders(
            connection.user_id,
            open_only=bool(data.get("open_only")),
            limit=int(data.get("limit") or 100),
        )
        connection.send_soon(encode(ServerMessage.ORDERS, {"orders": orders}, ref=ref))

    async def _handle_get_trades(
        self, connection: Connection, data: dict[str, Any], ref: str | None
    ) -> None:
        trades = await self.game.trading.list_trades(
            connection.user_id,
            limit=int(data.get("limit") or 100),
            symbol=data.get("symbol") or None,
        )
        connection.send_soon(encode(ServerMessage.TRADES, {"trades": trades}, ref=ref))

    async def _handle_get_leaderboard(
        self, connection: Connection, data: dict[str, Any], ref: str | None
    ) -> None:
        payload = self.game.leaderboard.payload(limit=int(data.get("limit") or 50))
        payload["your_rank"] = self.game.leaderboard.rank_of(connection.user_id)
        connection.send_soon(encode(ServerMessage.LEADERBOARD, payload, ref=ref))

    async def _handle_get_tips(
        self, connection: Connection, data: dict[str, Any], ref: str | None
    ) -> None:
        connection.send_soon(
            encode(ServerMessage.TIPS, await self._tips_payload(connection), ref=ref)
        )

    async def _tips_payload(self, connection: Connection) -> dict[str, Any]:
        settings = self.game.settings
        return {
            "tips": await self.game.insider.list_tips(connection.user_id),
            "min_fee": settings.insider_min_fee,
            "max_fee": settings.insider_max_fee,
            "enabled": settings.insider_enabled,
        }

    async def _handle_buy_tip(
        self, connection: Connection, data: dict[str, Any], ref: str | None
    ) -> None:
        await self.game.insider.buy_tip(connection.user_id, data.get("amount", 0))
        connection.send_soon(
            encode(ServerMessage.TIPS, await self._tips_payload(connection), ref=ref)
        )

    async def _handle_get_profile(
        self, connection: Connection, data: dict[str, Any], ref: str | None
    ) -> None:
        username = str(data.get("username") or connection.username)[:20]
        payload = await self.game.portfolios.get_profile(username, viewer_id=connection.user_id)
        payload["achievements"] = (
            await self.game.achievements.list_for(connection.user_id)
            if payload.get("is_self")
            else []
        )
        payload["rank"] = (
            self.game.leaderboard.rank_of(connection.user_id) if payload.get("is_self") else None
        )
        connection.send_soon(encode(ServerMessage.PROFILE, payload, ref=ref))

    async def _handle_get_news(
        self, connection: Connection, data: dict[str, Any], ref: str | None
    ) -> None:
        items = await self.game.market_data.news(
            limit=int(data.get("limit") or 40), symbol=data.get("symbol") or None
        )
        connection.send_soon(encode(ServerMessage.NEWS, {"items": items}, ref=ref))

    async def _handle_get_watchlist(
        self, connection: Connection, data: dict[str, Any], ref: str | None
    ) -> None:
        items = await self.game.portfolios.get_watchlist(connection.user_id)
        connection.send_soon(encode(ServerMessage.WATCHLIST, {"items": items}, ref=ref))

    async def _handle_watchlist_add(
        self, connection: Connection, data: dict[str, Any], ref: str | None
    ) -> None:
        items = await self.game.portfolios.add_to_watchlist(
            connection.user_id, str(data.get("symbol", ""))
        )
        connection.send_soon(encode(ServerMessage.WATCHLIST, {"items": items}, ref=ref))

    async def _handle_watchlist_remove(
        self, connection: Connection, data: dict[str, Any], ref: str | None
    ) -> None:
        items = await self.game.portfolios.remove_from_watchlist(
            connection.user_id, str(data.get("symbol", ""))
        )
        connection.send_soon(encode(ServerMessage.WATCHLIST, {"items": items}, ref=ref))

    # -- outbound -----------------------------------------------------------

    async def _on_tick(self, result: Any) -> None:
        """Fan out prices. Called once per tick from the game loop."""
        if not result.prices or not self.hub.total:
            return

        market_key = Channel.MARKET.key()
        if self.hub.has_subscribers(market_key):
            self.hub.send_to_channel(
                market_key,
                encode(
                    ServerMessage.PRICE_UPDATE,
                    {
                        "ticks": result.prices,
                        "index": result.index_value,
                        "index_change_pct": round(result.index_change_pct, 3),
                        "regime": str(result.regime.value) if result.regime else None,
                    },
                ),
            )

        # Per-symbol detail, but only for symbols somebody is actually looking
        # at -- usually one or two out of 47.
        watched = self.hub.active_symbol_channels()
        if watched:
            by_symbol = {tick["s"]: tick for tick in result.prices}
            for symbol in watched:
                tick = by_symbol.get(symbol)
                if tick is None:
                    continue
                self.hub.send_to_channel(
                    Channel.STOCK.key(symbol),
                    encode(
                        ServerMessage.PRICE_UPDATE,
                        {"symbol": symbol, "ticks": [tick], "detail": True},
                    ),
                )

        # Portfolios revalue on every tick, so push them on a slower cadence.
        if result.tick % max(1, int(2 / self.game.settings.tick_seconds) * 2) == 0:
            await self._push_portfolios_to_subscribers()

    async def _push_portfolios_to_subscribers(self) -> None:
        for connection in self.hub.subscribers(Channel.PORTFOLIO.key()):
            await self._push_portfolio(connection)

    async def _push_portfolio(self, connection: Connection, *, force: bool = False) -> None:
        now = asyncio.get_running_loop().time()
        if not force and now - connection.last_portfolio_push < PORTFOLIO_THROTTLE:
            return
        connection.last_portfolio_push = now
        try:
            payload = await self.game.portfolios.get_portfolio(connection.user_id)
        except GameError:
            return
        payload["rank"] = self.game.leaderboard.rank_of(connection.user_id)
        connection.send_soon(encode(ServerMessage.PORTFOLIO, payload))

    async def _on_order_updated(self, event: Event) -> None:
        if event.user_id is None:
            return
        self.hub.send_to_user(event.user_id, encode(ServerMessage.ORDER_UPDATE, event.payload))

    async def _on_trade_executed(self, event: Event) -> None:
        if event.user_id is None:
            return
        self.hub.send_to_user(event.user_id, encode(ServerMessage.TRADE_EXECUTED, event.payload))

    async def _on_portfolio_changed(self, event: Event) -> None:
        if event.user_id is None:
            return
        for connection in self.hub.connections_for(event.user_id):
            await self._push_portfolio(connection, force=True)

    async def _on_tape(self, event: Event) -> None:
        self.hub.send_to_channel(Channel.TAPE.key(), encode(ServerMessage.TAPE, event.payload))

    async def _on_news(self, event: Event) -> None:
        frame = encode(ServerMessage.NEWS_ITEM, event.payload)
        self.hub.send_to_channel(Channel.NEWS.key(), frame)
        # A headline about a stock also reaches whoever is watching that stock.
        symbol = event.payload.get("symbol")
        if symbol:
            self.hub.send_to_channel(Channel.STOCK.key(symbol), frame)

    async def _on_leaderboard(self, event: Event) -> None:
        key = Channel.LEADERBOARD.key()
        subscribers = self.hub.subscribers(key)
        if not subscribers:
            return
        # The board is identical for everyone, but "your rank" is not, so the
        # shared payload is built once and only that one field varies.
        board = self.game.leaderboard.payload()
        for connection in subscribers:
            connection.send_soon(
                encode(
                    ServerMessage.LEADERBOARD,
                    {**board, "your_rank": self.game.leaderboard.rank_of(connection.user_id)},
                )
            )

    def _announce_presence(self) -> None:
        """Tell everyone how many players are online.

        Sent on join and leave rather than on a timer: presence only changes
        when a socket opens or closes, so a timer would be pure noise.
        """
        key = Channel.STATUS.key()
        if not self.hub.has_subscribers(key):
            return
        self.hub.send_to_channel(
            key,
            encode(
                ServerMessage.MARKET_STATUS,
                {**self.game.engine.market_status(), "players_online": self.hub.unique_players},
            ),
        )

    async def _on_market_status(self, event: Event) -> None:
        self.hub.send_to_channel(
            Channel.STATUS.key(),
            encode(
                ServerMessage.MARKET_STATUS,
                {**event.payload, "players_online": self.hub.unique_players},
            ),
        )

    async def _on_achievement(self, event: Event) -> None:
        if event.user_id is None:
            return
        self.hub.send_to_user(
            event.user_id,
            encode(ServerMessage.OK, {"achievement": event.payload}),
        )


_HANDLERS = {
    ClientMessage.PLACE_ORDER.value: WebSocketGateway._handle_place_order,
    ClientMessage.CANCEL_ORDER.value: WebSocketGateway._handle_cancel_order,
    ClientMessage.GET_CANDLES.value: WebSocketGateway._handle_get_candles,
    ClientMessage.GET_PORTFOLIO.value: WebSocketGateway._handle_get_portfolio,
    ClientMessage.GET_ORDERS.value: WebSocketGateway._handle_get_orders,
    ClientMessage.GET_TRADES.value: WebSocketGateway._handle_get_trades,
    ClientMessage.GET_LEADERBOARD.value: WebSocketGateway._handle_get_leaderboard,
    ClientMessage.GET_PROFILE.value: WebSocketGateway._handle_get_profile,
    ClientMessage.GET_NEWS.value: WebSocketGateway._handle_get_news,
    ClientMessage.GET_WATCHLIST.value: WebSocketGateway._handle_get_watchlist,
    ClientMessage.GET_TIPS.value: WebSocketGateway._handle_get_tips,
    ClientMessage.BUY_TIP.value: WebSocketGateway._handle_buy_tip,
    ClientMessage.WATCHLIST_ADD.value: WebSocketGateway._handle_watchlist_add,
    ClientMessage.WATCHLIST_REMOVE.value: WebSocketGateway._handle_watchlist_remove,
}
