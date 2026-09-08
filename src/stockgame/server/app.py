"""FastAPI application factory.

``create_app`` builds a fully wired server. It is used by the CLI, by Docker
(``stockgame.server.app:app``) and by the test-suite, which drives the same
object through an ASGI transport rather than a real socket.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket
from fastapi.middleware.cors import CORSMiddleware

from stockgame.server.api.routes import build_router, register_exception_handlers
from stockgame.server.api.websocket import WebSocketGateway
from stockgame.server.config import Settings, get_settings
from stockgame.server.core.game import GameServer

log = logging.getLogger("stockgame.app")


def create_app(settings: Settings | None = None, *, run_loops: bool = True) -> FastAPI:
    settings = settings or get_settings()
    game = GameServer(settings)
    gateway = WebSocketGateway(game)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if settings.secret_key_is_ephemeral:
            log.warning(
                "STOCKGAME_SECRET_KEY is not set -- generated an ephemeral key. "
                "Set it in production or sessions will not survive a restart."
            )
        await game.start(run_loops=run_loops)
        try:
            yield
        finally:
            await gateway.hub.close_all()
            await game.stop()

    app = FastAPI(
        title="Stock Market Game",
        version="0.1.0",
        description=(
            "Authoritative server for a multiplayer terminal stock market "
            "simulator. Virtual currency only."
        ),
        lifespan=lifespan,
        docs_url="/docs" if settings.debug else None,
        redoc_url=None,
        openapi_url="/openapi.json" if settings.debug else None,
    )

    # Attached so tests and the admin CLI can reach the live objects.
    app.state.game = game
    app.state.gateway = gateway
    app.state.settings = settings

    if settings.cors_origin_list:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origin_list,
            allow_credentials=True,
            allow_methods=["GET", "POST"],
            allow_headers=["Authorization", "Content-Type"],
        )

    register_exception_handlers(app)
    app.include_router(build_router(game, gateway), prefix="/api")

    @app.websocket("/ws")
    async def websocket_endpoint(websocket: WebSocket) -> None:
        await gateway.handle(websocket)

    return app


_app: FastAPI | None = None


def __getattr__(name: str) -> FastAPI:
    """Lazily build the module-level ``app`` for ``uvicorn stockgame.server.app:app``.

    Deferred rather than constructed at import time so that importing this
    module (as the tests and the admin CLI do) does not open a database
    connection or read the ambient environment.
    """
    if name == "app":
        global _app
        if _app is None:
            _app = create_app()
        return _app
    raise AttributeError(name)
