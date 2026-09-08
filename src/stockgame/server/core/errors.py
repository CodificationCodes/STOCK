"""Domain exceptions.

Every one carries a :class:`ErrorCode` so the HTTP layer and the WebSocket
layer can translate failures identically without either of them needing to
know why something failed. Messages are written to be shown to the player.
"""

from __future__ import annotations

from stockgame.shared.protocol import ErrorCode


class GameError(Exception):
    """Base class for anything the player is allowed to be told about."""

    code: ErrorCode = ErrorCode.BAD_REQUEST
    http_status: int = 400

    def __init__(self, message: str, *, code: ErrorCode | None = None) -> None:
        super().__init__(message)
        self.message = message
        if code is not None:
            self.code = code


class AuthError(GameError):
    code = ErrorCode.UNAUTHENTICATED
    http_status = 401


class ForbiddenError(GameError):
    code = ErrorCode.FORBIDDEN
    http_status = 403


class NotFoundError(GameError):
    code = ErrorCode.NOT_FOUND
    http_status = 404


class ConflictError(GameError):
    code = ErrorCode.BAD_REQUEST
    http_status = 409


class RateLimitError(GameError):
    code = ErrorCode.RATE_LIMITED
    http_status = 429

    def __init__(self, message: str = "Too many requests. Slow down.", retry_after: float = 0.0):
        super().__init__(message)
        self.retry_after = retry_after


class InsufficientFundsError(GameError):
    code = ErrorCode.INSUFFICIENT_FUNDS


class InsufficientSharesError(GameError):
    code = ErrorCode.INSUFFICIENT_SHARES


class MarketClosedError(GameError):
    code = ErrorCode.MARKET_CLOSED


class InvalidOrderError(GameError):
    code = ErrorCode.INVALID_ORDER
