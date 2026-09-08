"""HTTP API.

Kept deliberately small: registration, login, logout and a few read-only
endpoints. Everything interactive happens over the WebSocket, because the
game needs push, not polling.

Admin routes are mounted only when ``STOCKGAME_ADMIN_ENABLED`` is true *and*
require a session belonging to a user with the admin flag. There is no
separate admin password, no default credentials and no HTML console.
"""

# NOTE: no ``from __future__ import annotations`` here. FastAPI evaluates
# route annotations at import time, and the dependency callables below are
# closures over ``build_router`` -- as PEP 563 strings they would be
# unresolvable and every ``Depends`` would silently degrade into a required
# query parameter.
import logging
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from pydantic import BaseModel, Field

from stockgame.server.core.errors import GameError, RateLimitError
from stockgame.server.core.game import GameServer
from stockgame.server.services.auth import AuthenticatedUser
from stockgame.shared.enums import MarketStatus
from stockgame.shared.validation import (
    PASSWORD_MIN,
    USERNAME_MAX,
    USERNAME_MIN,
    ValidationError,
)

log = logging.getLogger("stockgame.api")


class Credentials(BaseModel):
    username: str = Field(min_length=USERNAME_MIN, max_length=USERNAME_MAX)
    password: str = Field(min_length=PASSWORD_MIN, max_length=128)


class PasswordChange(BaseModel):
    current_password: str = Field(max_length=128)
    new_password: str = Field(min_length=PASSWORD_MIN, max_length=128)


class AuthResponse(BaseModel):
    token: str
    username: str
    expires_at: str
    is_admin: bool
    is_new_account: bool
    starting_cash_cents: int


def client_ip(request: Request, trust_proxy: bool) -> str:
    """Client address, honouring the proxy header only when configured to.

    Trusting ``X-Forwarded-For`` unconditionally would let anyone bypass rate
    limiting by inventing a header, so it is opt-in for deployments that
    actually sit behind a reverse proxy.
    """
    if trust_proxy:
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            return forwarded.split(",")[0].strip()[:64]
    return request.client.host if request.client else "unknown"


def build_router(game: GameServer, gateway) -> APIRouter:
    router = APIRouter()
    settings = game.settings

    async def current_user(
        authorization: Annotated[str | None, Header()] = None,
    ) -> AuthenticatedUser:
        if not authorization or not authorization.lower().startswith("bearer "):
            raise HTTPException(status_code=401, detail="Missing bearer token.")
        try:
            return await game.auth.authenticate(authorization[7:].strip())
        except GameError as exc:
            raise HTTPException(status_code=exc.http_status, detail=exc.message) from exc

    async def require_admin(
        user: Annotated[AuthenticatedUser, Depends(current_user)],
    ) -> AuthenticatedUser:
        if not user.is_admin:
            # 404 rather than 403: an unauthorised caller learns nothing about
            # whether these routes exist.
            raise HTTPException(status_code=404, detail="Not found.")
        return user

    def enforce_auth_limit(request: Request) -> None:
        key = client_ip(request, settings.trust_proxy_headers)
        result = game.auth_limiter.check(key)
        if not result.allowed:
            raise RateLimitError(
                f"Too many attempts. Try again in {result.retry_after:.0f} seconds.",
                retry_after=result.retry_after,
            )

    # -- public -------------------------------------------------------------

    @router.get("/health")
    async def health() -> dict[str, Any]:
        healthy = await game.db.healthcheck()
        return {
            "status": "ok" if healthy else "degraded",
            "database": healthy,
            "market": game.engine.status.value,
            "symbols": len(game.engine.sims),
        }

    @router.get("/info")
    async def info() -> dict[str, Any]:
        """Everything a client needs before logging in."""
        return {
            "name": "Stock Market Game",
            "protocol": 1,
            "starting_cash_cents": int(settings.starting_cash * 100),
            "symbols": len(game.engine.sims),
            "players_online": gateway.hub.unique_players,
            "market": game.engine.market_status(),
            "season": game.leaderboard.season_info(),
            "simulated": True,
            "notice": "Virtual currency only. No real money is involved.",
        }

    @router.post("/auth/register", response_model=AuthResponse)
    async def register(body: Credentials, request: Request) -> AuthResponse:
        enforce_auth_limit(request)
        result = await game.auth.register(
            body.username, body.password, client_info=request.headers.get("user-agent")
        )
        return AuthResponse(
            token=result.token,
            username=result.username,
            expires_at=result.expires_at.isoformat(),
            is_admin=result.is_admin,
            is_new_account=True,
            starting_cash_cents=result.starting_cash_cents,
        )

    @router.post("/auth/login", response_model=AuthResponse)
    async def login(body: Credentials, request: Request) -> AuthResponse:
        enforce_auth_limit(request)
        result = await game.auth.login(
            body.username, body.password, client_info=request.headers.get("user-agent")
        )
        # A correct password clears the throttle so a user who mistyped a few
        # times is not locked out of their own account.
        game.auth_limiter.reset(client_ip(request, settings.trust_proxy_headers))
        return AuthResponse(
            token=result.token,
            username=result.username,
            expires_at=result.expires_at.isoformat(),
            is_admin=result.is_admin,
            is_new_account=False,
            starting_cash_cents=result.starting_cash_cents,
        )

    @router.post("/auth/logout", status_code=204)
    async def logout(authorization: Annotated[str | None, Header()] = None) -> Response:
        if authorization and authorization.lower().startswith("bearer "):
            await game.auth.logout(authorization[7:].strip())
        return Response(status_code=204)

    @router.post("/auth/password", status_code=204)
    async def change_password(
        body: PasswordChange,
        user: Annotated[AuthenticatedUser, Depends(current_user)],
    ) -> Response:
        await game.auth.change_password(user.id, body.current_password, body.new_password)
        return Response(status_code=204)

    @router.get("/auth/me")
    async def me(user: Annotated[AuthenticatedUser, Depends(current_user)]) -> dict[str, Any]:
        return {"username": user.username, "is_admin": user.is_admin}

    # -- read-only game data ------------------------------------------------

    @router.get("/market")
    async def market(user: Annotated[AuthenticatedUser, Depends(current_user)]) -> dict[str, Any]:
        return {
            "stocks": game.engine.snapshot(),
            "sectors": game.engine.sector_performance(),
            "status": game.engine.market_status(),
        }

    @router.get("/market/{symbol}")
    async def stock(
        symbol: str, user: Annotated[AuthenticatedUser, Depends(current_user)]
    ) -> dict[str, Any]:
        return await game.portfolios.stock_detail(symbol)

    @router.get("/market/{symbol}/candles")
    async def candles(
        symbol: str,
        user: Annotated[AuthenticatedUser, Depends(current_user)],
        timeframe: str = "1D",
        points: int = 120,
    ) -> dict[str, Any]:
        return await game.market_data.candles(symbol, timeframe, points)

    @router.get("/portfolio")
    async def portfolio(
        user: Annotated[AuthenticatedUser, Depends(current_user)],
    ) -> dict[str, Any]:
        return await game.portfolios.get_portfolio(user.id)

    @router.get("/leaderboard")
    async def leaderboard(
        user: Annotated[AuthenticatedUser, Depends(current_user)], limit: int = 50
    ) -> dict[str, Any]:
        payload = game.leaderboard.payload(limit=min(limit, 100))
        payload["your_rank"] = game.leaderboard.rank_of(user.id)
        return payload

    @router.get("/players/{username}")
    async def profile(
        username: str, user: Annotated[AuthenticatedUser, Depends(current_user)]
    ) -> dict[str, Any]:
        return await game.portfolios.get_profile(username, viewer_id=user.id)

    @router.get("/news")
    async def news(
        user: Annotated[AuthenticatedUser, Depends(current_user)], limit: int = 40
    ) -> dict[str, Any]:
        return {"items": await game.market_data.news(limit=limit)}

    # -- admin --------------------------------------------------------------

    if settings.admin_enabled:
        admin = APIRouter(prefix="/admin", dependencies=[Depends(require_admin)])

        @admin.get("/status")
        async def admin_status() -> dict[str, Any]:
            status = game.status()
            status["connections"] = gateway.hub.stats()
            status["online"] = gateway.hub.online_usernames()
            status["trades"] = await game.trading.trade_count()
            return status

        @admin.post("/market/{action}")
        async def admin_market(action: str) -> dict[str, Any]:
            mapping = {
                "open": MarketStatus.OPEN,
                "close": MarketStatus.CLOSED,
                "halt": MarketStatus.HALTED,
            }
            if action not in mapping:
                raise HTTPException(status_code=400, detail="Use open, close or halt.")
            game.set_market_status(mapping[action])
            return {"status": game.engine.status.value}

        @admin.get("/players")
        async def admin_players(limit: int = 100) -> dict[str, Any]:
            valuations = await game.portfolios.value_all()
            valuations.sort(key=lambda v: v.total_value_cents, reverse=True)
            return {
                "players": [
                    {
                        "username": v.username,
                        "total_value_cents": v.total_value_cents,
                        "cash_cents": v.cash_cents,
                        "return_pct": round(v.total_return_pct, 2),
                    }
                    for v in valuations[:limit]
                ]
            }

        router.include_router(admin)

    return router


def register_exception_handlers(app) -> None:
    from fastapi.responses import JSONResponse

    @app.exception_handler(GameError)
    async def _game_error(request: Request, exc: GameError) -> JSONResponse:
        headers = {}
        if isinstance(exc, RateLimitError) and exc.retry_after:
            headers["Retry-After"] = str(int(exc.retry_after) + 1)
        return JSONResponse(
            status_code=exc.http_status,
            content={"code": str(exc.code), "detail": exc.message},
            headers=headers,
        )

    @app.exception_handler(ValidationError)
    async def _validation_error(request: Request, exc: ValidationError) -> JSONResponse:
        return JSONResponse(status_code=400, content={"code": "bad_request", "detail": str(exc)})
