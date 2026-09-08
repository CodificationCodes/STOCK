"""The price process.

Pure functions and plain dataclasses -- no database, no I/O -- so the model
can be unit tested and replayed deterministically from a seed.

Each tick a stock's log return is the sum of five effects:

1. **Drift** -- the company's long-run expected return.
2. **Market factor** -- one shared shock per tick, scaled by the stock's beta.
   This is what makes the whole board turn red together, and what lets
   ``AURM`` (negative beta) act as a hedge.
3. **Idiosyncratic diffusion** -- the stock's own noise, with GARCH-style
   volatility clustering so calm and turbulent stretches persist.
4. **Momentum** -- an EMA of recent returns, so trends have a little
   self-reinforcement before they exhaust.
5. **Mean reversion** -- a pull back toward a slowly-drifting fair value,
   which is what keeps prices from wandering to zero or infinity.

News events do not move the price directly; they move ``fair_value`` and
inject a decaying ``event_impact``, so a headline produces a sharp move
followed by a realistic partial retracement.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field

from stockgame.shared.enums import MarketRegime

#: Simulated trading days per simulated year.
TRADING_DAYS_PER_YEAR = 252

# Tuning constants. These were chosen so a typical stock moves ~1-3% on a
# quiet day and 5-15% on a big news day, which reads as believable.
#
# Everything here is expressed so that it behaves identically whether the
# model is stepped once per simulated day (history seeding) or 1800 times per
# simulated day (live ticks). Momentum in particular is an EMA *of returns*
# rather than a fixed per-step nudge -- an absolute nudge would compound into
# an explosion once the step count went up.
MOMENTUM_DECAY = 0.94
#: Fraction of the recent average return fed back as trend. Above ~0.4 this
#: stops being momentum and starts being a runaway.
MOMENTUM_WEIGHT = 0.18
#: Momentum is capped in units of one step's own sigma.
MOMENTUM_CAP_SIGMAS = 3.0
#: Per-day pull toward fair value, as a fraction of the log gap. 0.02 with a
#: mean_reversion around 1.0 gives a half-life of roughly five weeks -- long
#: enough that a stock can stay mispriced and tradeable.
REVERSION_SCALE = 0.02
#: Fundamental uncertainty: fair value is itself a random walk, scaled off the
#: stock's volatility. Without this every company converges on its drift and
#: the leaderboard becomes a coin-flip about who bought the highest beta.
FAIR_VALUE_VOL_SHARE = 0.6
EVENT_DECAY = 0.90
#: E|N(0,1)|, so the volatility-clustering state centres on 1.0.
_MEAN_ABS_NORMAL = 0.7978845608
VOL_CLUSTER_PERSISTENCE = 0.97
VOL_CLUSTER_REACTION = 0.03
VOL_CLUSTER_MIN = 0.65
VOL_CLUSTER_MAX = 2.60
MAX_TICK_RETURN = 0.06  # circuit breaker on a single tick
MIN_PRICE_CENTS = 25


@dataclass(slots=True)
class RegimeProfile:
    """How a market regime bends drift, volatility and news sentiment."""

    drift: float
    vol_multiplier: float
    #: Probability weight applied to positive news vs negative.
    news_bias: float
    #: Expected duration in simulated days, used when re-rolling.
    mean_days: float


REGIME_PROFILES: dict[MarketRegime, RegimeProfile] = {
    MarketRegime.EUPHORIA: RegimeProfile(
        drift=0.38, vol_multiplier=1.15, news_bias=0.72, mean_days=9
    ),
    MarketRegime.BULL: RegimeProfile(drift=0.18, vol_multiplier=0.92, news_bias=0.60, mean_days=32),
    MarketRegime.NEUTRAL: RegimeProfile(
        drift=0.05, vol_multiplier=1.00, news_bias=0.50, mean_days=40
    ),
    MarketRegime.BEAR: RegimeProfile(
        drift=-0.16, vol_multiplier=1.35, news_bias=0.38, mean_days=24
    ),
    MarketRegime.PANIC: RegimeProfile(
        drift=-0.55, vol_multiplier=2.20, news_bias=0.24, mean_days=6
    ),
}

#: Regime transition weights. Panic decays into bear far more often than it
#: flips straight to bull, which is what gives crashes their long tail.
REGIME_TRANSITIONS: dict[MarketRegime, dict[MarketRegime, float]] = {
    MarketRegime.EUPHORIA: {
        MarketRegime.BULL: 0.45,
        MarketRegime.NEUTRAL: 0.25,
        MarketRegime.BEAR: 0.22,
        MarketRegime.PANIC: 0.08,
    },
    MarketRegime.BULL: {
        MarketRegime.EUPHORIA: 0.14,
        MarketRegime.NEUTRAL: 0.62,
        MarketRegime.BEAR: 0.22,
        MarketRegime.PANIC: 0.02,
    },
    MarketRegime.NEUTRAL: {
        MarketRegime.BULL: 0.40,
        MarketRegime.BEAR: 0.36,
        MarketRegime.EUPHORIA: 0.10,
        MarketRegime.PANIC: 0.14,
    },
    MarketRegime.BEAR: {
        MarketRegime.NEUTRAL: 0.55,
        MarketRegime.PANIC: 0.17,
        MarketRegime.BULL: 0.28,
    },
    MarketRegime.PANIC: {
        MarketRegime.BEAR: 0.62,
        MarketRegime.NEUTRAL: 0.33,
        MarketRegime.BULL: 0.05,
    },
}


@dataclass(slots=True)
class StockSim:
    """Mutable per-stock simulation state (mirrors the ``stocks`` row)."""

    symbol: str
    sector: str
    price_cents: int
    fair_value_cents: int
    volatility: float
    drift: float
    beta: float
    liquidity: float
    mean_reversion: float
    momentum: float = 0.0
    event_impact: float = 0.0
    #: GARCH-ish multiplier on this stock's own volatility.
    vol_state: float = 1.0
    #: Additive drift from an active sector rotation, decays daily.
    sector_tilt: float = 0.0
    day_volume: int = 0
    is_halted: bool = False

    @property
    def price(self) -> float:
        return self.price_cents / 100


@dataclass(slots=True)
class MarketSim:
    """Shared market-wide state for one tick."""

    regime: MarketRegime = MarketRegime.NEUTRAL
    regime_ticks_left: int = 0
    #: Per-sector additive drift, refreshed on rotation events.
    sector_tilts: dict[str, float] = field(default_factory=dict)
    #: Market-wide decaying shock from a global news event.
    event_impact: float = 0.0

    @property
    def profile(self) -> RegimeProfile:
        return REGIME_PROFILES[self.regime]


def tick_dt(ticks_per_day: int) -> float:
    """Fraction of a simulated *year* elapsed in one tick."""
    return 1.0 / (TRADING_DAYS_PER_YEAR * max(ticks_per_day, 1))


def market_factor(market: MarketSim, rng: random.Random, dt: float) -> float:
    """The common shock every stock shares this tick.

    Returned as a log return already scaled for ``dt``.
    """
    profile = market.profile
    # Market index volatility is lower than a single stock's: diversification.
    sigma = 0.16 * profile.vol_multiplier
    shock = rng.gauss(0.0, 1.0) * sigma * math.sqrt(dt)
    market.event_impact *= EVENT_DECAY
    return profile.drift * dt + shock + market.event_impact


def step_stock(
    stock: StockSim,
    market: MarketSim,
    factor: float,
    rng: random.Random,
    dt: float,
) -> float:
    """Advance one stock by a tick. Returns the realised log return.

    Mutates ``stock`` in place (price, momentum, volatility state, volume).
    """
    if stock.is_halted:
        return 0.0

    # 3. Idiosyncratic diffusion with volatility clustering.
    sigma = stock.volatility * stock.vol_state * market.profile.vol_multiplier
    idio = rng.gauss(0.0, 1.0) * sigma * math.sqrt(dt)

    # 5. Mean reversion toward fair value (in log space). Scaled per *day* so
    # the pull is identical at any tick resolution.
    gap = math.log(max(stock.price_cents, 1) / max(stock.fair_value_cents, 1))
    reversion = -stock.mean_reversion * gap * REVERSION_SCALE * dt * TRADING_DAYS_PER_YEAR

    tilt = market.sector_tilts.get(stock.sector, 0.0) + stock.sector_tilt

    log_return = (
        (stock.drift + tilt) * dt  # 1. company drift + sector rotation
        + stock.beta * factor  # 2. market factor
        + idio  # 3. own noise
        + stock.momentum * MOMENTUM_WEIGHT  # 4. trend persistence
        + reversion  # 5. pull to fair value
        + stock.event_impact  # decaying news shock
    )
    log_return = max(-MAX_TICK_RETURN, min(MAX_TICK_RETURN, log_return))

    new_price = stock.price_cents * math.exp(log_return)
    stock.price_cents = max(MIN_PRICE_CENTS, round(new_price))

    # Momentum is an EMA of realised returns, so it is automatically on the
    # same scale as one step and cannot compound out of control.
    step_sigma = max(sigma * math.sqrt(dt), 1e-12)
    stock.momentum = _clamp(
        MOMENTUM_DECAY * stock.momentum + (1 - MOMENTUM_DECAY) * log_return,
        -MOMENTUM_CAP_SIGMAS * step_sigma,
        MOMENTUM_CAP_SIGMAS * step_sigma,
    )
    # Big moves beget big moves; quiet ticks bleed volatility back to normal.
    normalised = abs(log_return) / step_sigma / _MEAN_ABS_NORMAL
    stock.vol_state = _clamp(
        VOL_CLUSTER_PERSISTENCE * stock.vol_state
        + VOL_CLUSTER_REACTION * normalised
        + (1 - VOL_CLUSTER_PERSISTENCE - VOL_CLUSTER_REACTION),
        VOL_CLUSTER_MIN,
        VOL_CLUSTER_MAX,
    )
    stock.event_impact *= EVENT_DECAY
    stock.day_volume += simulated_volume(stock, log_return, rng)

    # Fair value is a random walk of its own: the business genuinely gets
    # better or worse, which is what creates long-run winners and losers.
    fv_sigma = stock.volatility * FAIR_VALUE_VOL_SHARE
    fv_return = (stock.drift + tilt) * dt + rng.gauss(0.0, 1.0) * fv_sigma * math.sqrt(dt)
    stock.fair_value_cents = max(
        MIN_PRICE_CENTS,
        round(stock.fair_value_cents * math.exp(fv_return)),
    )
    return log_return


def simulated_volume(stock: StockSim, log_return: float, rng: random.Random) -> int:
    """Volume for one tick: a liquidity-scaled base, amplified by movement."""
    base = 40 + 900 * stock.liquidity
    excitement = 1.0 + min(abs(log_return) * 220, 9.0)
    noise = rng.lognormvariate(0.0, 0.55)
    return int(base * excitement * noise)


def roll_regime(market: MarketSim, rng: random.Random, ticks_per_day: int) -> MarketRegime:
    """Pick the next regime and how long it lasts."""
    transitions = REGIME_TRANSITIONS[market.regime]
    choices = list(transitions.keys())
    weights = list(transitions.values())
    market.regime = rng.choices(choices, weights=weights, k=1)[0]
    mean_days = REGIME_PROFILES[market.regime].mean_days
    # Exponential dwell time gives regimes a memoryless, unpredictable end.
    days = max(1.0, rng.expovariate(1.0 / mean_days))
    market.regime_ticks_left = int(days * ticks_per_day)
    return market.regime


def apply_shock(stock: StockSim, impact: float, *, fair_value_share: float = 0.6) -> None:
    """Apply a news shock.

    ``fair_value_share`` of the move is treated as a permanent re-rating of
    the business; the remainder is a temporary dislocation that mean reversion
    will claw back. Illiquid names gap harder on the same news.
    """
    illiquidity_multiplier = 1.0 + (1.0 - stock.liquidity) * 0.8
    effective = impact * illiquidity_multiplier
    stock.fair_value_cents = max(
        MIN_PRICE_CENTS,
        round(stock.fair_value_cents * math.exp(effective * fair_value_share)),
    )
    # The transient part is injected as a decaying per-tick push, spread over
    # roughly 1/(1-EVENT_DECAY) ticks so the move looks like a real reaction
    # rather than a single-frame teleport.
    stock.event_impact += effective * (1 - EVENT_DECAY)
    stock.vol_state = min(VOL_CLUSTER_MAX, stock.vol_state + abs(effective) * 6)


def apply_market_shock(market: MarketSim, impact: float) -> None:
    market.event_impact += impact * (1 - EVENT_DECAY)


def _clamp(value: float, low: float, high: float) -> float:
    return low if value < low else high if value > high else value
