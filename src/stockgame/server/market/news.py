"""Simulated financial news.

News is the visible face of the simulation: a headline is generated *with*
the shock it causes, so a player who reads the tape can reason about what
just happened to their positions.

Three scopes:

* ``COMPANY`` -- hits one stock hard (earnings, trials, contracts, scandals).
* ``SECTOR``  -- tilts a whole sector's drift for several days (rotations).
* ``MARKET``  -- moves everything, and can flip the market regime.

Templates carry an impact *range*; the actual magnitude is drawn per event
and modulated by the prevailing regime, so the same headline lands harder in
a panic than in a calm market.
"""

from __future__ import annotations

import random
from dataclasses import dataclass

from stockgame.shared.enums import NewsScope, NewsSentiment, Sector


@dataclass(frozen=True, slots=True)
class NewsTemplate:
    headline: str
    body: str
    sentiment: NewsSentiment
    #: Log-return impact range; sign comes from the sentiment.
    impact_min: float
    impact_max: float


@dataclass(slots=True)
class GeneratedNews:
    """A concrete headline ready to be applied and persisted."""

    scope: NewsScope
    sentiment: NewsSentiment
    headline: str
    body: str
    impact: float
    symbol: str | None = None
    sector: str | None = None


# ---------------------------------------------------------------------------
# Company-level templates. {name} and {symbol} are substituted.
# ---------------------------------------------------------------------------

COMPANY_GOOD: tuple[NewsTemplate, ...] = (
    NewsTemplate(
        "{name} beats earnings expectations",
        "{name} reported quarterly results ahead of consensus, with management raising full-year guidance.",
        NewsSentiment.POSITIVE,
        0.03,
        0.11,
    ),
    NewsTemplate(
        "{name} wins landmark contract",
        "A multi-year agreement is expected to add materially to {symbol}'s order book.",
        NewsSentiment.POSITIVE,
        0.02,
        0.08,
    ),
    NewsTemplate(
        "{name} announces buyback programme",
        "The board authorised a repurchase of up to 8% of shares outstanding.",
        NewsSentiment.POSITIVE,
        0.01,
        0.05,
    ),
    NewsTemplate(
        "Analysts upgrade {name} to Buy",
        "Two major desks raised {symbol} on improving margins and pricing power.",
        NewsSentiment.POSITIVE,
        0.01,
        0.045,
    ),
    NewsTemplate(
        "{name} unveils breakthrough product",
        "Early demand indications for the new line have exceeded internal forecasts.",
        NewsSentiment.POSITIVE,
        0.03,
        0.13,
    ),
    NewsTemplate(
        "{name} expands into new markets",
        "The company confirmed operations will begin in three additional regions next quarter.",
        NewsSentiment.POSITIVE,
        0.015,
        0.06,
    ),
    NewsTemplate(
        "Takeover speculation lifts {name}",
        "Press reports suggest a private consortium has approached the board about {symbol}.",
        NewsSentiment.POSITIVE,
        0.05,
        0.17,
    ),
    NewsTemplate(
        "{name} raises full-year guidance",
        "Management now expects revenue growth at the upper end of the prior range.",
        NewsSentiment.POSITIVE,
        0.02,
        0.075,
    ),
)

COMPANY_BAD: tuple[NewsTemplate, ...] = (
    NewsTemplate(
        "{name} misses earnings estimates",
        "Results came in below consensus and management withdrew full-year guidance.",
        NewsSentiment.NEGATIVE,
        0.03,
        0.12,
    ),
    NewsTemplate(
        "{name} faces regulatory probe",
        "Regulators confirmed an inquiry into {symbol}'s accounting practices.",
        NewsSentiment.NEGATIVE,
        0.04,
        0.15,
    ),
    NewsTemplate(
        "{name} issues profit warning",
        "Cost inflation and weak demand will hit margins, the company said.",
        NewsSentiment.NEGATIVE,
        0.04,
        0.14,
    ),
    NewsTemplate(
        "{name} loses key customer",
        "The contract represented a meaningful share of {symbol}'s recurring revenue.",
        NewsSentiment.NEGATIVE,
        0.02,
        0.08,
    ),
    NewsTemplate(
        "Analysts downgrade {name}",
        "Coverage was cut to Underweight on valuation and competitive pressure.",
        NewsSentiment.NEGATIVE,
        0.01,
        0.05,
    ),
    NewsTemplate(
        "{name} announces surprise CEO exit",
        "The chief executive will step down immediately; no successor has been named.",
        NewsSentiment.NEGATIVE,
        0.02,
        0.09,
    ),
    NewsTemplate(
        "Production halted at {name} facility",
        "An unplanned outage will reduce output for at least six weeks.",
        NewsSentiment.NEGATIVE,
        0.025,
        0.10,
    ),
    NewsTemplate(
        "{name} dilutes shareholders with equity raise",
        "The placement was priced at a double-digit discount to the last close.",
        NewsSentiment.NEGATIVE,
        0.03,
        0.11,
    ),
)

# Sector-flavoured company news, keyed by sector for extra colour.
SECTOR_FLAVOUR: dict[Sector, tuple[NewsTemplate, ...]] = {
    Sector.ENERGY: (
        NewsTemplate(
            "{name} announces major lithium discovery",
            "Initial drilling results point to one of the largest finds in a decade.",
            NewsSentiment.POSITIVE,
            0.06,
            0.19,
        ),
        NewsTemplate(
            "{name} project delayed by permitting",
            "First production slips by at least four quarters.",
            NewsSentiment.NEGATIVE,
            0.03,
            0.11,
        ),
    ),
    Sector.HEALTHCARE: (
        NewsTemplate(
            "{name} trial hits primary endpoint",
            "Phase III data showed statistically significant benefit; filing expected within months.",
            NewsSentiment.POSITIVE,
            0.08,
            0.26,
        ),
        NewsTemplate(
            "{name} trial fails to meet endpoint",
            "The candidate showed no benefit over standard of care. The programme is discontinued.",
            NewsSentiment.NEGATIVE,
            0.10,
            0.31,
        ),
    ),
    Sector.TECHNOLOGY: (
        NewsTemplate(
            "{name} secures flagship platform partnership",
            "The deal embeds {symbol}'s stack across a top-tier customer's estate.",
            NewsSentiment.POSITIVE,
            0.04,
            0.13,
        ),
        NewsTemplate(
            "{name} discloses major security breach",
            "Customer data was accessed; regulators have been notified.",
            NewsSentiment.NEGATIVE,
            0.05,
            0.16,
        ),
    ),
    Sector.MINING: (
        NewsTemplate(
            "{name} upgrades resource estimate",
            "Measured and indicated tonnage rose 40% after infill drilling.",
            NewsSentiment.POSITIVE,
            0.05,
            0.16,
        ),
        NewsTemplate(
            "Mine incident suspends {name} operations",
            "Operations are suspended pending an investigation.",
            NewsSentiment.NEGATIVE,
            0.05,
            0.18,
        ),
    ),
    Sector.FINANCE: (
        NewsTemplate(
            "{name} reports record net interest income",
            "Deposit costs stabilised while loan yields continued to reprice higher.",
            NewsSentiment.POSITIVE,
            0.02,
            0.07,
        ),
        NewsTemplate(
            "{name} takes large credit provision",
            "Rising defaults in the commercial book forced a sizeable charge.",
            NewsSentiment.NEGATIVE,
            0.03,
            0.10,
        ),
    ),
    Sector.CONSUMER: (
        NewsTemplate(
            "{name} posts strong same-store sales",
            "Foot traffic and basket size both improved year on year.",
            NewsSentiment.POSITIVE,
            0.02,
            0.07,
        ),
        NewsTemplate(
            "{name} issues product recall",
            "The recall covers three product lines across all regions.",
            NewsSentiment.NEGATIVE,
            0.03,
            0.10,
        ),
    ),
    Sector.INDUSTRIAL: (
        NewsTemplate(
            "{name} backlog hits record high",
            "Book-to-bill exceeded 1.3x for a third consecutive quarter.",
            NewsSentiment.POSITIVE,
            0.02,
            0.08,
        ),
        NewsTemplate(
            "{name} warns on supply chain costs",
            "Component shortages will push deliveries into next year.",
            NewsSentiment.NEGATIVE,
            0.02,
            0.09,
        ),
    ),
    Sector.COMMUNICATIONS: (
        NewsTemplate(
            "{name} adds record subscribers",
            "Net additions beat guidance and churn fell to an all-time low.",
            NewsSentiment.POSITIVE,
            0.03,
            0.10,
        ),
        NewsTemplate(
            "{name} loses key content rights",
            "A rival outbid the company for its most-watched property.",
            NewsSentiment.NEGATIVE,
            0.03,
            0.11,
        ),
    ),
}

SECTOR_NEWS: tuple[NewsTemplate, ...] = (
    NewsTemplate(
        "{sector} stocks rally on sector optimism",
        "Investors rotated into {sector} names following supportive industry data.",
        NewsSentiment.POSITIVE,
        0.01,
        0.05,
    ),
    NewsTemplate(
        "{sector} sector under pressure",
        "A broad de-rating hit {sector} names as sentiment soured.",
        NewsSentiment.NEGATIVE,
        0.01,
        0.055,
    ),
    NewsTemplate(
        "Regulators announce {sector} crackdown",
        "Proposed rules would raise compliance costs across the {sector} sector.",
        NewsSentiment.NEGATIVE,
        0.02,
        0.07,
    ),
    NewsTemplate(
        "Government stimulus targets {sector}",
        "A funding package is expected to accelerate {sector} investment.",
        NewsSentiment.POSITIVE,
        0.02,
        0.075,
    ),
    NewsTemplate(
        "Capital rotates into {sector}",
        "Fund flow data showed the largest weekly inflow into {sector} this year.",
        NewsSentiment.POSITIVE,
        0.01,
        0.045,
    ),
)

MARKET_NEWS: tuple[NewsTemplate, ...] = (
    NewsTemplate(
        "Global markets fall amid recession fears",
        "Weak leading indicators triggered a broad risk-off move across every sector.",
        NewsSentiment.NEGATIVE,
        0.015,
        0.06,
    ),
    NewsTemplate(
        "Markets surge on rate cut hopes",
        "Softer inflation data revived expectations of policy easing.",
        NewsSentiment.POSITIVE,
        0.015,
        0.055,
    ),
    NewsTemplate(
        "Central bank raises rates unexpectedly",
        "The surprise hike sent risk assets sharply lower.",
        NewsSentiment.NEGATIVE,
        0.02,
        0.07,
    ),
    NewsTemplate(
        "Blowout jobs report lifts sentiment",
        "Employment came in far ahead of forecasts, easing recession worries.",
        NewsSentiment.POSITIVE,
        0.01,
        0.045,
    ),
    NewsTemplate(
        "Liquidity crunch grips credit markets",
        "Funding spreads widened sharply, forcing broad de-risking.",
        NewsSentiment.NEGATIVE,
        0.03,
        0.09,
    ),
    NewsTemplate(
        "Record inflows into equities",
        "Retail and institutional flows both turned decisively positive.",
        NewsSentiment.POSITIVE,
        0.01,
        0.05,
    ),
)

EARNINGS_GOOD = NewsTemplate(
    "{name} reports stronger-than-expected earnings",
    "Revenue and margins both beat consensus for the quarter.",
    NewsSentiment.POSITIVE,
    0.02,
    0.10,
)
EARNINGS_BAD = NewsTemplate(
    "{name} earnings disappoint",
    "The quarter fell short on both revenue and profitability.",
    NewsSentiment.NEGATIVE,
    0.02,
    0.11,
)


class NewsGenerator:
    """Draws headlines. Owns no state beyond its RNG."""

    def __init__(self, rng: random.Random) -> None:
        self.rng = rng

    def company_news(
        self,
        symbol: str,
        name: str,
        sector: Sector,
        *,
        positive: bool | None = None,
        bias: float = 0.5,
    ) -> GeneratedNews:
        if positive is None:
            positive = self.rng.random() < bias
        pool: list[NewsTemplate] = list(COMPANY_GOOD if positive else COMPANY_BAD)
        flavour = SECTOR_FLAVOUR.get(sector)
        if flavour:
            # Sector-specific headlines are rarer but much punchier.
            matching = [t for t in flavour if (t.sentiment is NewsSentiment.POSITIVE) == positive]
            pool.extend(matching * 2)
        template = self.rng.choice(pool)
        return self._build(template, NewsScope.COMPANY, symbol=symbol, name=name, sector=sector)

    def earnings(self, symbol: str, name: str, sector: Sector, *, beat: bool) -> GeneratedNews:
        template = EARNINGS_GOOD if beat else EARNINGS_BAD
        return self._build(template, NewsScope.COMPANY, symbol=symbol, name=name, sector=sector)

    def sector_news(self, sector: Sector, *, bias: float = 0.5) -> GeneratedNews:
        positive = self.rng.random() < bias
        pool = [t for t in SECTOR_NEWS if (t.sentiment is NewsSentiment.POSITIVE) == positive]
        template = self.rng.choice(pool or list(SECTOR_NEWS))
        return self._build(template, NewsScope.SECTOR, sector=sector)

    def market_news(self, *, bias: float = 0.5) -> GeneratedNews:
        positive = self.rng.random() < bias
        pool = [t for t in MARKET_NEWS if (t.sentiment is NewsSentiment.POSITIVE) == positive]
        template = self.rng.choice(pool or list(MARKET_NEWS))
        return self._build(template, NewsScope.MARKET)

    def _build(
        self,
        template: NewsTemplate,
        scope: NewsScope,
        *,
        symbol: str | None = None,
        name: str | None = None,
        sector: Sector | None = None,
    ) -> GeneratedNews:
        magnitude = self.rng.uniform(template.impact_min, template.impact_max)
        impact = magnitude if template.sentiment is NewsSentiment.POSITIVE else -magnitude
        fields = {
            "symbol": symbol or "",
            "name": name or symbol or "",
            "sector": str(sector.value) if sector else "",
        }
        return GeneratedNews(
            scope=scope,
            sentiment=template.sentiment,
            headline=template.headline.format(**fields),
            body=template.body.format(**fields),
            impact=impact,
            symbol=symbol,
            sector=str(sector.value) if sector else None,
        )
