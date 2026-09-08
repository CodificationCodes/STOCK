"""Input rules shared by both sides.

The client uses these to give instant feedback; the server uses the *same*
functions to actually enforce them. Client-side checks are a convenience,
never a gate -- every value is re-validated server-side before it is trusted.
"""

from __future__ import annotations

import re

USERNAME_MIN = 3
USERNAME_MAX = 20
PASSWORD_MIN = 8
PASSWORD_MAX = 128
MAX_ORDER_QUANTITY = 1_000_000

_USERNAME_RE = re.compile(r"^[A-Za-z0-9_]+$")
_SYMBOL_RE = re.compile(r"^[A-Z]{2,6}$")


class ValidationError(ValueError):
    """A user-supplied value failed a rule. The message is safe to display."""


def validate_username(username: str) -> str:
    if not isinstance(username, str):
        raise ValidationError("Username must be text.")
    username = username.strip()
    if len(username) < USERNAME_MIN or len(username) > USERNAME_MAX:
        raise ValidationError(f"Username must be {USERNAME_MIN}-{USERNAME_MAX} characters.")
    if not _USERNAME_RE.match(username):
        raise ValidationError("Username may only contain letters, numbers and underscores.")
    return username


def validate_password(password: str) -> str:
    if not isinstance(password, str):
        raise ValidationError("Password must be text.")
    if len(password) < PASSWORD_MIN:
        raise ValidationError(f"Password must be at least {PASSWORD_MIN} characters.")
    if len(password) > PASSWORD_MAX:
        # Long inputs are rejected rather than truncated: hashing an unbounded
        # string is a cheap denial-of-service vector.
        raise ValidationError(f"Password must be at most {PASSWORD_MAX} characters.")
    if password.isdigit() or password.isalpha():
        raise ValidationError("Password must mix letters with numbers or symbols.")
    return password


def validate_symbol(symbol: str) -> str:
    if not isinstance(symbol, str):
        raise ValidationError("Symbol must be text.")
    symbol = symbol.strip().upper()
    if not _SYMBOL_RE.match(symbol):
        raise ValidationError("Symbol must be 2-6 letters.")
    return symbol


def validate_quantity(quantity: object) -> int:
    if isinstance(quantity, bool) or not isinstance(quantity, (int, float, str)):
        raise ValidationError("Quantity must be a whole number.")
    try:
        qty = int(str(quantity).strip().replace(",", ""))
    except (TypeError, ValueError) as exc:
        raise ValidationError("Quantity must be a whole number.") from exc
    if qty <= 0:
        raise ValidationError("Quantity must be greater than zero.")
    if qty > MAX_ORDER_QUANTITY:
        raise ValidationError(f"Quantity may not exceed {MAX_ORDER_QUANTITY:,} shares.")
    return qty


def validate_price(price: object) -> float:
    """Validate a limit price expressed in dollars."""
    if isinstance(price, bool) or not isinstance(price, (int, float, str)):
        raise ValidationError("Price must be a number.")
    try:
        value = float(str(price).strip().replace(",", "").lstrip("$"))
    except (TypeError, ValueError) as exc:
        raise ValidationError("Price must be a number.") from exc
    if value <= 0:
        raise ValidationError("Price must be greater than zero.")
    if value > 1_000_000:
        raise ValidationError("Price is out of range.")
    return value
