"""Money handling and display formatting.

Money is stored and transported as **integer cents**. Floats are never used
for balances or trade maths -- only for chart geometry and price simulation,
where the result is quantised back to cents before it touches an account.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

CENTS = Decimal("0.01")


def to_cents(value: float | int | str | Decimal) -> int:
    """Quantise a price/amount to integer cents (half-up, like an exchange)."""
    return int(Decimal(str(value)).quantize(CENTS, rounding=ROUND_HALF_UP) * 100)


def from_cents(cents: int) -> Decimal:
    """Exact decimal dollars for display or further exact arithmetic."""
    return (Decimal(cents) / 100).quantize(CENTS)


def fmt_money(cents: int, *, sign: bool = False, compact: bool = False) -> str:
    """``1234567 -> '$12,345.67'``; ``compact`` gives ``'$12.35K'``."""
    negative = cents < 0
    magnitude = abs(cents)
    body = _compact(magnitude / 100) if compact else f"{magnitude / 100:,.2f}"
    prefix = "-" if negative else ("+" if sign and cents > 0 else "")
    return f"{prefix}${body}"


def _compact(dollars: float) -> str:
    for limit, suffix in ((1e12, "T"), (1e9, "B"), (1e6, "M"), (1e3, "K")):
        if dollars >= limit:
            return f"{dollars / limit:.2f}{suffix}"
    return f"{dollars:,.2f}"


def fmt_pct(pct: float, *, sign: bool = True, places: int = 2) -> str:
    """Format an already-computed percentage (``2.4 -> '+2.40%'``)."""
    prefix = "+" if sign and pct > 0 else ""
    return f"{prefix}{pct:.{places}f}%"


def fmt_qty(qty: int) -> str:
    return f"{qty:,}"


def pct_change(current: int, previous: int) -> float:
    """Percentage change between two cent amounts; 0.0 when undefined."""
    if previous == 0:
        return 0.0
    return (current - previous) / previous * 100.0
