"""Password hashing and session tokens.

No cryptography is implemented here. Password hashing is delegated to
``argon2-cffi`` (Argon2id, the PHC winner) and token generation to
``secrets``. The only bespoke logic is *what* gets stored:

* Passwords -> Argon2id hash, with automatic rehash when parameters change.
* Session tokens -> a 256-bit random string returned to the client once;
  only its SHA-256 digest is persisted, so a database leak does not hand an
  attacker a set of working sessions.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError

from stockgame.server.config import Settings

TOKEN_BYTES = 32
PUBLIC_ID_BYTES = 16


class PasswordService:
    """Wraps Argon2id with the configured cost parameters."""

    def __init__(self, settings: Settings) -> None:
        self._hasher = PasswordHasher(
            time_cost=settings.argon2_time_cost,
            memory_cost=settings.argon2_memory_cost,
            parallelism=settings.argon2_parallelism,
        )

    def hash(self, password: str) -> str:
        return self._hasher.hash(password)

    def verify(self, password_hash: str, password: str) -> bool:
        """Constant-time-ish verification; never raises on a bad password."""
        try:
            return self._hasher.verify(password_hash, password)
        except (VerifyMismatchError, InvalidHashError, ValueError):
            return False

    def needs_rehash(self, password_hash: str) -> bool:
        try:
            return self._hasher.check_needs_rehash(password_hash)
        except (InvalidHashError, ValueError):
            return True


def generate_token() -> str:
    """A fresh session token. Shown to the client exactly once."""
    return secrets.token_urlsafe(TOKEN_BYTES)


def hash_token(token: str) -> str:
    """Digest stored in the ``sessions`` table.

    A plain SHA-256 is correct here: the token already has 256 bits of
    entropy, so there is nothing for a slow KDF to protect against.
    """
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def compare_digest(left: str, right: str) -> bool:
    return hmac.compare_digest(left, right)


def public_id() -> str:
    """Opaque identifier for orders and trades, safe to show to clients.

    Sequential integer IDs would leak the platform's total order count and
    let players correlate each other's activity.
    """
    return secrets.token_urlsafe(PUBLIC_ID_BYTES)[:22]


def dummy_verify(password_service: PasswordService) -> None:
    """Burn a hash cycle so a login for an unknown user costs the same as a
    login for a known one, closing the timing oracle on username existence."""
    password_service.verify(_DUMMY_HASH, "not-the-password")


# A pre-computed hash of a random string; only used to equalise login timing.
_DUMMY_HASH = (
    "$argon2id$v=19$m=65536,t=3,p=2$c3RvY2tnYW1lZHVtbXlzYWx0$"
    "0mZ2S1nEkYcEwvGRPmpbLdvJvXG0YHZlqLtQe0EKBpc"
)
