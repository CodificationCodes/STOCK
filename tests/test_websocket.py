"""WebSocket protocol, subscriptions and multiplayer isolation.

Most cases drive :class:`WebSocketGateway` through a fake socket, which
exercises the real handshake, dispatch and fan-out logic without a network.
One test at the bottom goes through the actual ASGI route to prove the
transport is wired up too.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from fastapi import WebSocketDisconnect

from stockgame.server.api.websocket import WebSocketGateway
from stockgame.shared.protocol import (
    PROTOCOL_VERSION,
    ClientMessage,
    ErrorCode,
    ServerMessage,
    encode,
)


class FakeSocket:
    """Minimal stand-in for Starlette's WebSocket."""

    def __init__(self, inbound: list[str] | None = None) -> None:
        self.inbound: asyncio.Queue[str] = asyncio.Queue()
        for frame in inbound or []:
            self.inbound.put_nowait(frame)
        self.sent: list[dict] = []
        self.accepted = False
        self.closed_with: int | None = None
        self._lingered = False

    async def accept(self) -> None:
        self.accepted = True

    async def send_text(self, data: str) -> None:
        self.sent.append(json.loads(data))

    async def receive_text(self) -> str:
        if self.inbound.empty():
            # Replies are queued and written by a separate writer task. Yield
            # once before disconnecting so that task gets to flush, otherwise
            # the test would assert against responses that were merely queued.
            if not self._lingered:
                self._lingered = True
                await asyncio.sleep(0.05)
                if not self.inbound.empty():
                    return await self.inbound.get()
            raise WebSocketDisconnect(1000)
        return await self.inbound.get()

    async def close(self, code: int = 1000) -> None:
        self.closed_with = code

    # -- helpers used by the tests -----------------------------------------

    def push(self, kind, data=None, msg_id=None) -> None:
        self.inbound.put_nowait(encode(kind, data or {}, msg_id=msg_id))

    def of_type(self, kind) -> list[dict]:
        return [frame for frame in self.sent if frame["type"] == str(kind)]

    def first(self, kind) -> dict | None:
        found = self.of_type(kind)
        return found[0] if found else None


@pytest.fixture
def gateway(app) -> WebSocketGateway:
    return app.state.gateway


async def run_socket(gateway: WebSocketGateway, socket: FakeSocket) -> FakeSocket:
    """Drive one connection to completion (the fake disconnects when drained)."""
    await gateway.handle(socket)
    return socket


def hello(token: str) -> str:
    return encode(ClientMessage.HELLO, {"token": token, "client": "pytest"})


class TestHandshake:
    async def test_valid_token_is_welcomed(self, gateway, player):
        socket = await run_socket(gateway, FakeSocket([hello(player["token"])]))
        welcome = socket.first(ServerMessage.WELCOME)
        assert welcome is not None
        assert welcome["data"]["username"] == "alice"
        assert welcome["data"]["protocol"] == PROTOCOL_VERSION
        assert welcome["data"]["starting_cash_cents"] == 10_000_000

    async def test_bad_token_is_refused_and_closed(self, gateway):
        socket = await run_socket(gateway, FakeSocket([hello("not-a-real-token")]))
        error = socket.first(ServerMessage.ERROR)
        assert error["data"]["code"] == ErrorCode.UNAUTHENTICATED.value
        assert socket.closed_with == 1008
        assert socket.of_type(ServerMessage.WELCOME) == []

    async def test_a_non_hello_first_frame_is_refused(self, gateway, player):
        socket = await run_socket(gateway, FakeSocket([encode(ClientMessage.GET_PORTFOLIO, {})]))
        assert socket.first(ServerMessage.ERROR)["data"]["code"] == (
            ErrorCode.UNAUTHENTICATED.value
        )

    async def test_protocol_mismatch_is_reported_clearly(self, gateway, player):
        frame = json.loads(hello(player["token"]))
        frame["v"] = PROTOCOL_VERSION + 99
        socket = await run_socket(gateway, FakeSocket([json.dumps(frame)]))
        assert socket.first(ServerMessage.ERROR)["data"]["code"] == (
            ErrorCode.PROTOCOL_MISMATCH.value
        )

    async def test_malformed_json_is_refused(self, gateway):
        socket = await run_socket(gateway, FakeSocket(["{not json"]))
        assert socket.first(ServerMessage.ERROR) is not None

    async def test_the_connection_is_deregistered_on_disconnect(self, gateway, player):
        await run_socket(gateway, FakeSocket([hello(player["token"])]))
        assert gateway.hub.total == 0


class TestSubscriptions:
    async def test_subscribing_to_market_sends_a_snapshot(self, gateway, player):
        socket = FakeSocket([hello(player["token"])])
        socket.push(ClientMessage.SUBSCRIBE, {"channels": ["market"]})
        await run_socket(gateway, socket)

        snapshot = socket.first(ServerMessage.MARKET_SNAPSHOT)
        assert snapshot is not None
        assert len(snapshot["data"]["stocks"]) == 47
        assert snapshot["data"]["sectors"]

    async def test_subscribing_to_a_stock_sends_its_detail(self, gateway, player):
        socket = FakeSocket([hello(player["token"])])
        socket.push(ClientMessage.SUBSCRIBE, {"channels": ["stock:ACME"]})
        await run_socket(gateway, socket)

        detail = socket.first(ServerMessage.STOCK_SNAPSHOT)
        assert detail["data"]["symbol"] == "ACME"
        assert detail["data"]["price_cents"] > 0

    async def test_unknown_channels_are_ignored_not_fatal(self, gateway, player):
        socket = FakeSocket([hello(player["token"])])
        socket.push(ClientMessage.SUBSCRIBE, {"channels": ["nonsense", "stock:ZZZZ", "market"]})
        await run_socket(gateway, socket)
        assert socket.first(ServerMessage.MARKET_SNAPSHOT) is not None

    async def test_portfolio_channel_sends_only_your_own(self, gateway, player, rival, buy):
        await buy(player["user_id"], "ACME", 10)
        socket = FakeSocket([hello(rival["token"])])
        socket.push(ClientMessage.SUBSCRIBE, {"channels": ["portfolio"]})
        await run_socket(gateway, socket)

        portfolio = socket.first(ServerMessage.PORTFOLIO)
        assert portfolio["data"]["username"] == "bob"
        assert portfolio["data"]["positions"] == []


class TestRequests:
    async def test_requests_are_answered_with_a_matching_ref(self, gateway, player):
        socket = FakeSocket([hello(player["token"])])
        socket.push(ClientMessage.GET_PORTFOLIO, {}, msg_id="req-1")
        await run_socket(gateway, socket)

        reply = socket.first(ServerMessage.PORTFOLIO)
        assert reply["ref"] == "req-1"
        assert reply["data"]["cash_cents"] == 10_000_000

    async def test_ping_pong(self, gateway, player):
        socket = FakeSocket([hello(player["token"])])
        socket.push(ClientMessage.PING, {"t": 42}, msg_id="p1")
        await run_socket(gateway, socket)
        assert socket.first(ServerMessage.PONG)["data"]["t"] == 42

    async def test_placing_an_order_over_the_socket(self, gateway, game, player):
        socket = FakeSocket([hello(player["token"])])
        socket.push(
            ClientMessage.PLACE_ORDER,
            {"symbol": "ACME", "side": "buy", "type": "market", "quantity": 25},
            msg_id="o1",
        )
        await run_socket(gateway, socket)

        reply = [f for f in socket.of_type(ServerMessage.ORDER_UPDATE) if f.get("ref") == "o1"]
        assert reply and reply[0]["data"]["order"]["status"] == "filled"

        portfolio = await game.portfolios.get_portfolio(player["user_id"])
        assert portfolio["positions"][0]["quantity"] == 25

    async def test_an_invalid_order_returns_an_error_not_a_disconnect(self, gateway, player):
        socket = FakeSocket([hello(player["token"])])
        socket.push(
            ClientMessage.PLACE_ORDER,
            {"symbol": "ACME", "side": "buy", "type": "market", "quantity": -5},
            msg_id="bad",
        )
        socket.push(ClientMessage.PING, {"t": 1}, msg_id="after")
        await run_socket(gateway, socket)

        assert any(f.get("ref") == "bad" for f in socket.of_type(ServerMessage.ERROR))
        # The socket kept working after the rejection.
        assert socket.first(ServerMessage.PONG) is not None

    async def test_insufficient_funds_is_reported_with_its_code(self, gateway, player):
        socket = FakeSocket([hello(player["token"])])
        socket.push(
            ClientMessage.PLACE_ORDER,
            {"symbol": "ACME", "side": "buy", "type": "market", "quantity": 999_999},
        )
        await run_socket(gateway, socket)
        assert socket.first(ServerMessage.ERROR)["data"]["code"] == (
            ErrorCode.INSUFFICIENT_FUNDS.value
        )

    async def test_watchlist_round_trip(self, gateway, player):
        socket = FakeSocket([hello(player["token"])])
        socket.push(ClientMessage.WATCHLIST_ADD, {"symbol": "QNTM"}, msg_id="a")
        socket.push(ClientMessage.WATCHLIST_REMOVE, {"symbol": "ACME"}, msg_id="r")
        await run_socket(gateway, socket)

        frames = socket.of_type(ServerMessage.WATCHLIST)
        symbols_after_add = {row["symbol"] for row in frames[0]["data"]["items"]}
        symbols_after_remove = {row["symbol"] for row in frames[-1]["data"]["items"]}
        assert "QNTM" in symbols_after_add
        assert "ACME" not in symbols_after_remove

    async def test_candles_can_be_requested(self, gateway, player):
        socket = FakeSocket([hello(player["token"])])
        socket.push(ClientMessage.GET_CANDLES, {"symbol": "ACME", "timeframe": "1M", "points": 30})
        await run_socket(gateway, socket)
        candles = socket.first(ServerMessage.CANDLES)
        assert candles["data"]["symbol"] == "ACME"
        assert candles["data"]["candles"]

    async def test_unknown_message_types_are_rejected(self, gateway, player):
        socket = FakeSocket([hello(player["token"])])
        socket.push("summon_money", {"amount": 1_000_000}, msg_id="hax")
        await run_socket(gateway, socket)
        assert socket.first(ServerMessage.ERROR)["data"]["code"] == ErrorCode.BAD_REQUEST.value

    async def test_oversized_frames_are_rejected(self, gateway, player):
        socket = FakeSocket([hello(player["token"])])
        socket.inbound.put_nowait("x" * 20_000)
        await run_socket(gateway, socket)
        assert socket.first(ServerMessage.ERROR) is not None


class TestAntiCheatOverTheWire:
    async def test_a_frame_cannot_claim_another_users_identity(self, gateway, game, player, rival):
        socket = FakeSocket([hello(player["token"])])
        socket.push(
            ClientMessage.PLACE_ORDER,
            {
                "symbol": "ACME",
                "side": "buy",
                "type": "market",
                "quantity": 10,
                "user_id": rival["user_id"],
                "username": "bob",
            },
        )
        await run_socket(gateway, socket)

        assert (await game.portfolios.get_portfolio(rival["user_id"]))["positions"] == []
        assert (await game.portfolios.get_portfolio(player["user_id"]))["positions"]

    async def test_you_cannot_request_another_players_portfolio(self, gateway, player, rival):
        socket = FakeSocket([hello(player["token"])])
        socket.push(ClientMessage.GET_PORTFOLIO, {"username": "bob", "user_id": rival["user_id"]})
        await run_socket(gateway, socket)
        assert socket.first(ServerMessage.PORTFOLIO)["data"]["username"] == "alice"

    async def test_another_players_profile_omits_private_data(self, gateway, player, rival):
        socket = FakeSocket([hello(player["token"])])
        socket.push(ClientMessage.GET_PROFILE, {"username": "bob"})
        await run_socket(gateway, socket)

        profile = socket.first(ServerMessage.PROFILE)["data"]
        assert profile["username"] == "bob"
        assert profile["is_self"] is False
        assert profile["achievements"] == []
        assert "cash_cents" not in profile


class TestMultiplayerFanOut:
    async def test_two_players_can_be_connected_at_once(self, gateway, player, rival):
        alice = FakeSocket([hello(player["token"])])
        bob = FakeSocket([hello(rival["token"])])

        # Hold both sockets open by feeding them a slow trickle.
        async def hold(socket, frames):
            for frame in frames:
                socket.inbound.put_nowait(frame)
                await asyncio.sleep(0)
            await gateway.handle(socket)

        await asyncio.gather(
            hold(alice, [encode(ClientMessage.PING, {"t": 1})]),
            hold(bob, [encode(ClientMessage.PING, {"t": 2})]),
        )
        assert alice.first(ServerMessage.WELCOME)["data"]["username"] == "alice"
        assert bob.first(ServerMessage.WELCOME)["data"]["username"] == "bob"

    async def test_price_updates_reach_market_subscribers_only(self, gateway, game, player):
        """A tick must not spray prices at clients that did not ask for them."""
        subscribed = gateway.hub.add(FakeSocket(), player["user_id"], "alice", False)
        silent = gateway.hub.add(FakeSocket(), player["user_id"], "alice", False)
        gateway.hub.subscribe(subscribed, "market")

        result = await game.tick_once()
        await gateway._on_tick(result)

        assert subscribed.queue.qsize() >= 1
        assert silent.queue.qsize() == 0

        frame = json.loads(subscribed.queue.get_nowait())
        assert frame["type"] == ServerMessage.PRICE_UPDATE.value
        assert len(frame["data"]["ticks"]) == 47

    async def test_a_trade_prints_to_the_public_tape(self, gateway, game, player, rival, buy):
        watcher = gateway.hub.add(FakeSocket(), rival["user_id"], "bob", False)
        gateway.hub.subscribe(watcher, "tape")

        await buy(player["user_id"], "ACME", 15)

        frames = [json.loads(watcher.queue.get_nowait()) for _ in range(watcher.queue.qsize())]
        tape = [f for f in frames if f["type"] == ServerMessage.TAPE.value]
        assert tape
        printed = tape[0]["data"]
        assert printed["username"] == "alice"
        assert printed["symbol"] == "ACME"
        assert printed["quantity"] == 15
        # The tape says what happened, never how rich anyone is.
        assert "cash_cents" not in printed
        assert "realized_pl_cents" not in printed

    async def test_private_fills_go_only_to_the_owner(self, gateway, game, player, rival, buy):
        mine = gateway.hub.add(FakeSocket(), player["user_id"], "alice", False)
        theirs = gateway.hub.add(FakeSocket(), rival["user_id"], "bob", False)

        await buy(player["user_id"], "ACME", 5)

        my_frames = [json.loads(mine.queue.get_nowait()) for _ in range(mine.queue.qsize())]
        their_frames = [json.loads(theirs.queue.get_nowait()) for _ in range(theirs.queue.qsize())]
        assert any(f["type"] == ServerMessage.TRADE_EXECUTED.value for f in my_frames)
        assert not any(f["type"] == ServerMessage.TRADE_EXECUTED.value for f in their_frames)

    async def test_news_reaches_news_subscribers(self, gateway, game, player):
        listener = gateway.hub.add(FakeSocket(), player["user_id"], "alice", False)
        gateway.hub.subscribe(listener, "news")

        from stockgame.server.core.events import Event, Topics

        await game.bus.publish(
            Event(
                Topics.NEWS_PUBLISHED,
                {
                    "headline": "Test headline",
                    "impact_pct": 5.0,
                    "symbol": "ACME",
                    "scope": "company",
                },
            )
        )
        frame = json.loads(listener.queue.get_nowait())
        assert frame["type"] == ServerMessage.NEWS_ITEM.value
        assert frame["data"]["headline"] == "Test headline"

    async def test_a_slow_client_is_dropped_rather_than_buffered_forever(self, gateway, player):
        connection = gateway.hub.add(FakeSocket(), player["user_id"], "alice", False)
        gateway.hub.subscribe(connection, "market")
        for _ in range(500):
            gateway.hub.send_to_channel("market", '{"type":"noise"}')
        # It was evicted instead of growing an unbounded queue.
        assert gateway.hub.total == 0


class TestRealTransport:
    """One pass through the genuine ASGI WebSocket route."""

    def test_connect_subscribe_and_trade_over_a_real_socket(self, settings):
        from starlette.testclient import TestClient

        from stockgame.server.app import create_app

        application = create_app(settings, run_loops=False)
        with TestClient(application) as client:
            registered = client.post(
                "/api/auth/register", json={"username": "socketeer", "password": "correct-horse-7"}
            )
            token = registered.json()["token"]

            with client.websocket_connect("/ws") as socket:
                socket.send_text(encode(ClientMessage.HELLO, {"token": token}))
                welcome = json.loads(socket.receive_text())
                assert welcome["type"] == ServerMessage.WELCOME.value
                assert welcome["data"]["username"] == "socketeer"

                socket.send_text(encode(ClientMessage.SUBSCRIBE, {"channels": ["market"]}))
                snapshot = json.loads(socket.receive_text())
                assert snapshot["type"] == ServerMessage.MARKET_SNAPSHOT.value
                assert len(snapshot["data"]["stocks"]) == 47

                socket.send_text(
                    encode(
                        ClientMessage.PLACE_ORDER,
                        {"symbol": "ACME", "side": "buy", "type": "market", "quantity": 10},
                        msg_id="buy-1",
                    )
                )
                for _ in range(12):
                    frame = json.loads(socket.receive_text())
                    if frame.get("ref") == "buy-1":
                        assert frame["data"]["order"]["status"] == "filled"
                        break
                else:
                    pytest.fail("no reply to the order")
