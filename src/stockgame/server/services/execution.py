"""The execution venue.

The MVP does not run a real central limit order book -- players trade against
a simulated market maker whose quote is derived from the engine price. What
it *does* model, and what actually matters for game feel, is:

* a bid/ask spread that widens on illiquid names,
* size-dependent slippage, so dumping 50,000 shares costs you,
* permanent market impact, so a large order visibly moves the tape for
  everyone else -- this is what makes the market feel shared,
* partial fills for resting orders, limited by how much size trades per tick.

:class:`ExecutionVenue` is a narrow interface. Replacing it with a real
matching engine means implementing the same three methods against a book;
nothing in the trading service needs to change.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass
from typing import Protocol

from stockgame.shared.enums import OrderSide

#: Half-spread in basis points at perfect (1.0) and zero liquidity.
MIN_HALF_SPREAD = 0.0005
MAX_EXTRA_SPREAD = 0.0025
#: Shares that trade without moving the price, at liquidity 1.0.
BASE_DEPTH_SHARES = 5_000
#: Slippage coefficient: a full-depth order costs about this fraction.
IMPACT_COEFFICIENT = 0.004
#: Share of the slippage that sticks as a permanent price move.
PERMANENT_IMPACT_SHARE = 0.35
#: Hard cap so a single order cannot dislocate a stock beyond belief.
MAX_SLIPPAGE = 0.12


@dataclass(slots=True)
class Fill:
    quantity: int
    price_cents: int
    #: Log-return the order pushed into the market price, for the engine.
    permanent_impact: float


class ExecutionVenue(Protocol):
    """What the trading service needs from an execution backend."""

    def quote(self, symbol: str, side: OrderSide, quantity: int) -> int:
        """Price per share for an immediate order of this size."""

    def fill_market(self, symbol: str, side: OrderSide, quantity: int) -> Fill | None:
        """Execute immediately; ``None`` if the symbol cannot trade."""

    def available_size(self, symbol: str) -> int:
        """Shares a resting order may fill this tick."""


def depth_shares(liquidity: float) -> float:
    return BASE_DEPTH_SHARES * max(liquidity, 0.05) + 500


def half_spread(liquidity: float) -> float:
    return MIN_HALF_SPREAD + MAX_EXTRA_SPREAD * (1.0 - max(0.0, min(1.0, liquidity)))


def slippage(quantity: int, liquidity: float) -> float:
    """Fractional price concession for an order of ``quantity`` shares.

    Square-root impact: the standard empirical shape, and it means doubling
    your size costs only ~1.4x more, which keeps large trades viable but
    never free.
    """
    size_ratio = quantity / depth_shares(liquidity)
    impact = IMPACT_COEFFICIENT * math.sqrt(max(size_ratio, 0.0))
    return min(MAX_SLIPPAGE, half_spread(liquidity) + impact)


class SimulatedVenue:
    """Execution against the market engine's live price."""

    def __init__(self, engine, rng: random.Random | None = None) -> None:
        self.engine = engine
        self.rng = rng or random.Random()

    def quote(self, symbol: str, side: OrderSide, quantity: int) -> int:
        sim = self.engine.sims.get(symbol)
        if sim is None:
            raise KeyError(symbol)
        concession = slippage(quantity, sim.liquidity)
        direction = 1 if side is OrderSide.BUY else -1
        return max(1, round(sim.price_cents * (1 + direction * concession)))

    def fill_market(self, symbol: str, side: OrderSide, quantity: int) -> Fill | None:
        sim = self.engine.sims.get(symbol)
        if sim is None or sim.is_halted:
            return None
        concession = slippage(quantity, sim.liquidity)
        direction = 1 if side is OrderSide.BUY else -1
        price = max(1, round(sim.price_cents * (1 + direction * concession)))
        permanent = direction * concession * PERMANENT_IMPACT_SHARE
        self._apply_impact(sim, permanent, quantity)
        return Fill(quantity=quantity, price_cents=price, permanent_impact=permanent)

    def fill_limit(
        self, symbol: str, side: OrderSide, quantity: int, limit_cents: int
    ) -> Fill | None:
        """Fill a resting order at the better of its limit and the market.

        Passive orders pay no spread -- they *were* the liquidity.
        """
        sim = self.engine.sims.get(symbol)
        if sim is None or sim.is_halted:
            return None
        if side is OrderSide.BUY:
            if sim.price_cents > limit_cents:
                return None
            price = min(limit_cents, sim.price_cents)
        else:
            if sim.price_cents < limit_cents:
                return None
            price = max(limit_cents, sim.price_cents)
        permanent = (
            (1 if side is OrderSide.BUY else -1)
            * slippage(quantity, sim.liquidity)
            * PERMANENT_IMPACT_SHARE
            * 0.5  # resting orders absorb rather than demand liquidity
        )
        self._apply_impact(sim, permanent, quantity)
        return Fill(quantity=quantity, price_cents=max(1, price), permanent_impact=permanent)

    def available_size(self, symbol: str) -> int:
        """How much a resting order can fill this tick.

        Randomised so identical orders do not fill in lockstep, which would
        make the book feel mechanical.
        """
        sim = self.engine.sims.get(symbol)
        if sim is None:
            return 0
        base = depth_shares(sim.liquidity) * 0.25
        return max(1, int(base * self.rng.lognormvariate(0.0, 0.7)))

    def _apply_impact(self, sim, permanent: float, quantity: int) -> None:
        """Push the engine price and add the trade to reported volume."""
        if permanent:
            sim.price_cents = max(25, round(sim.price_cents * math.exp(permanent)))
        sim.day_volume += quantity
