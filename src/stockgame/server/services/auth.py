"""Registration, login, sessions.

The only place in the codebase that reads or writes credentials.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, func, select

from stockgame.server.config import Settings
from stockgame.server.core.errors import AuthError, ConflictError, ForbiddenError
from stockgame.server.core.security import (
    PasswordService,
    dummy_verify,
    generate_token,
    hash_token,
)
from stockgame.server.db.models import Portfolio, Session, User, WatchlistItem
from stockgame.server.db.session import Database
from stockgame.shared.money import to_cents
from stockgame.shared.validation import validate_password, validate_username

log = logging.getLogger("stockgame.auth")

#: Symbols added to a new player's watchlist so the screen is never empty.
STARTER_WATCHLIST = ("ACME", "NOVA", "BYTE", "AERO", "MEDX")


@dataclass(slots=True)
class AuthenticatedUser:
    """The identity attached to a request or socket. No secrets."""

    id: int
    username: str
    is_admin: bool
    session_id: int


@dataclass(slots=True)
class LoginResult:
    token: str
    expires_at: datetime
    user_id: int
    username: str
    is_admin: bool
    is_new: bool = False
    starting_cash_cents: int = 0


class AuthService:
    def __init__(self, db: Database, settings: Settings) -> None:
        self.db = db
        self.settings = settings
        self.passwords = PasswordService(settings)

    # -- registration -------------------------------------------------------

    async def register(
        self, username: str, password: str, *, client_info: str | None = None
    ) -> LoginResult:
        username = validate_username(username)
        validate_password(password)
        password_hash = self.passwords.hash(password)
        starting_cash = to_cents(self.settings.starting_cash)

        async with self.db.write_session() as session:
            existing = await session.scalar(
                select(User.id).where(User.username_lower == username.lower())
            )
            if existing is not None:
                raise ConflictError("That username is already taken.")

            # The very first account to register becomes the admin. After that
            # the flag can only be granted with the stockgame-admin CLI.
            user_count = await session.scalar(select(func.count()).select_from(User))
            user = User(
                username=username,
                username_lower=username.lower(),
                password_hash=password_hash,
                is_admin=user_count == 0,
            )
            session.add(user)
            await session.flush()

            session.add(
                Portfolio(
                    user_id=user.id,
                    cash_cents=starting_cash,
                    deposited_cents=starting_cash,
                    last_value_cents=starting_cash,
                    day_open_value_cents=starting_cash,
                )
            )
            await self._seed_watchlist(session, user.id)
            token, expires_at = await self._issue_session(session, user, client_info)
            log.info("registered user=%s id=%s admin=%s", user.username, user.id, user.is_admin)
            return LoginResult(
                token=token,
                expires_at=expires_at,
                user_id=user.id,
                username=user.username,
                is_admin=user.is_admin,
                is_new=True,
                starting_cash_cents=starting_cash,
            )

    async def _seed_watchlist(self, session, user_id: int) -> None:
        from stockgame.server.db.models import Stock

        rows = (
            await session.execute(
                select(Stock.id, Stock.symbol).where(Stock.symbol.in_(STARTER_WATCHLIST))
            )
        ).all()
        for order, (stock_id, _symbol) in enumerate(rows):
            session.add(WatchlistItem(user_id=user_id, stock_id=stock_id, sort_order=order))

    # -- login / logout -----------------------------------------------------

    async def login(
        self, username: str, password: str, *, client_info: str | None = None
    ) -> LoginResult:
        # Deliberately vague: never reveal whether the username exists.
        failure = AuthError("Incorrect username or password.")
        try:
            username = validate_username(username)
        except ValueError:
            dummy_verify(self.passwords)
            raise failure from None

        async with self.db.write_session() as session:
            user = await session.scalar(select(User).where(User.username_lower == username.lower()))
            if user is None:
                dummy_verify(self.passwords)
                raise failure
            if not self.passwords.verify(user.password_hash, password):
                raise failure
            if not user.is_active:
                raise ForbiddenError("This account has been suspended.")

            # Transparently upgrade the hash if the cost parameters changed.
            if self.passwords.needs_rehash(user.password_hash):
                user.password_hash = self.passwords.hash(password)

            user.last_login_at = datetime.now(timezone.utc)
            token, expires_at = await self._issue_session(session, user, client_info)
            portfolio = await session.scalar(select(Portfolio).where(Portfolio.user_id == user.id))
            log.info("login user=%s id=%s", user.username, user.id)
            return LoginResult(
                token=token,
                expires_at=expires_at,
                user_id=user.id,
                username=user.username,
                is_admin=user.is_admin,
                starting_cash_cents=portfolio.cash_cents if portfolio else 0,
            )

    async def _issue_session(
        self, session, user: User, client_info: str | None
    ) -> tuple[str, datetime]:
        token = generate_token()
        expires_at = datetime.now(timezone.utc) + timedelta(hours=self.settings.session_ttl_hours)
        session.add(
            Session(
                user_id=user.id,
                token_hash=hash_token(token),
                expires_at=expires_at,
                client_info=(client_info or "")[:120] or None,
            )
        )
        return token, expires_at

    async def logout(self, token: str) -> None:
        async with self.db.write_session() as session:
            await session.execute(delete(Session).where(Session.token_hash == hash_token(token)))

    async def logout_all(self, user_id: int) -> int:
        async with self.db.write_session() as session:
            result = await session.execute(delete(Session).where(Session.user_id == user_id))
            return result.rowcount or 0

    # -- verification -------------------------------------------------------

    async def authenticate(self, token: str) -> AuthenticatedUser:
        """Resolve a bearer token to an identity, or raise :class:`AuthError`."""
        if not token or not isinstance(token, str) or len(token) > 256:
            raise AuthError("Missing or malformed token.")

        digest = hash_token(token)
        now = datetime.now(timezone.utc)
        async with self.db.session() as session:
            row = (
                await session.execute(
                    select(Session, User)
                    .join(User, Session.user_id == User.id)
                    .where(Session.token_hash == digest)
                )
            ).first()
            if row is None:
                raise AuthError("Session not found. Please log in again.")
            db_session, user = row
            if db_session.revoked:
                raise AuthError("Session revoked. Please log in again.")
            if db_session.expires_at <= now:
                raise AuthError("Session expired. Please log in again.")
            if not user.is_active:
                raise ForbiddenError("This account has been suspended.")
            return AuthenticatedUser(
                id=user.id,
                username=user.username,
                is_admin=user.is_admin,
                session_id=db_session.id,
            )

    async def touch_session(self, session_id: int) -> None:
        """Record activity. Best-effort; never blocks a request."""
        from sqlalchemy import update

        async with self.db.write_session() as session:
            await session.execute(
                update(Session)
                .where(Session.id == session_id)
                .values(last_seen_at=datetime.now(timezone.utc))
            )

    async def change_password(self, user_id: int, current: str, new_password: str) -> None:
        validate_password(new_password)
        async with self.db.write_session() as session:
            user = await session.get(User, user_id)
            if user is None:
                raise AuthError("Account not found.")
            if not self.passwords.verify(user.password_hash, current):
                raise AuthError("Current password is incorrect.")
            user.password_hash = self.passwords.hash(new_password)
            user.token_epoch += 1
            # Changing a password logs every other device out.
            await session.execute(delete(Session).where(Session.user_id == user_id))
            log.info("password changed user=%s", user_id)

    async def purge_expired_sessions(self) -> int:
        async with self.db.write_session() as session:
            result = await session.execute(
                delete(Session).where(Session.expires_at <= datetime.now(timezone.utc))
            )
            return result.rowcount or 0
