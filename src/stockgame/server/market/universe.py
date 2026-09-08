"""The listed universe: 47 fictional companies across eight sectors.

Every company is deliberately given a distinct personality through four
parameters, so the market has genuine texture rather than 47 copies of the
same random walk:

``volatility``  annualised sigma -- how violently it swings
``drift``       annualised expected return -- underlying business quality
``beta``        sensitivity to market-wide moves
``liquidity``   0..1; thin names slip more on size and gap harder on news

Prices here are only the *genesis* values. Once the market is seeded the
database is authoritative and this table is never re-applied.
"""

from __future__ import annotations

from dataclasses import dataclass

from stockgame.shared.enums import Sector


@dataclass(frozen=True, slots=True)
class Listing:
    symbol: str
    name: str
    sector: Sector
    price: float
    volatility: float
    drift: float
    beta: float
    liquidity: float
    shares_outstanding: int
    description: str


def _m(millions: float) -> int:
    return int(millions * 1_000_000)


# fmt: off
# The alignment here is deliberate: this table is meant to be read as
# columns, so the formatter is turned off for it.
UNIVERSE: tuple[Listing, ...] = (
    # -- Technology: high drift, high volatility, mostly liquid ---------------
    Listing("ACME", "Acme Technologies", Sector.TECHNOLOGY, 142.37, 0.38, 0.14, 1.25, 0.95,
            _m(420), "Diversified computing platforms and developer tooling."),
    Listing("BYTE", "Byte Systems", Sector.TECHNOLOGY, 38.12, 0.44, 0.11, 1.35, 0.88,
            _m(310), "Edge servers and storage fabric for hyperscale customers."),
    Listing("QNTM", "Quantum Core Labs", Sector.TECHNOLOGY, 87.60, 0.72, 0.22, 1.70, 0.55,
            _m(95), "Pre-revenue quantum processors. Spectacular either way."),
    Listing("NEXA", "Nexa Software", Sector.TECHNOLOGY, 216.45, 0.31, 0.13, 1.10, 0.92,
            _m(180), "Enterprise workflow suite with a stubbornly loyal install base."),
    Listing("PIXL", "Pixel Foundry", Sector.TECHNOLOGY, 24.88, 0.58, 0.09, 1.45, 0.62,
            _m(140), "Game engines, rendering pipelines and creative tooling."),
    Listing("SILC", "Silicon Straits", Sector.TECHNOLOGY, 64.20, 0.49, 0.16, 1.40, 0.80,
            _m(260), "Contract fabrication for analogue and mixed-signal chips."),
    Listing("HELX", "Helix Compute", Sector.TECHNOLOGY, 311.05, 0.41, 0.19, 1.30, 0.86,
            _m(150), "Accelerators for scientific and machine-learning workloads."),

    # -- Energy: cyclical, event-driven --------------------------------------
    Listing("NOVA", "Nova Energy", Sector.ENERGY, 76.42, 0.46, 0.08, 1.15, 0.78,
            _m(220), "Utility-scale storage and grid balancing."),
    Listing("VOLT", "Voltaic Power", Sector.ENERGY, 52.30, 0.40, 0.06, 1.05, 0.82,
            _m(340), "Regulated generation with a slow, dependable dividend profile."),
    Listing("HELI", "Heliostat Solar", Sector.ENERGY, 19.74, 0.63, 0.10, 1.50, 0.58,
            _m(190), "Concentrated solar plants in three continents."),
    Listing("PTRO", "Petro Meridian", Sector.ENERGY, 118.90, 0.42, 0.03, 0.95, 0.90,
            _m(410), "Upstream crude and refined products. Moves with the barrel."),
    Listing("FUSE", "Fusion Dynamics", Sector.ENERGY, 41.15, 0.85, 0.24, 1.85, 0.40,
            _m(70), "Net-positive fusion, allegedly eighteen months away since 2019."),
    Listing("GALE", "Gale Offshore", Sector.ENERGY, 33.60, 0.51, 0.07, 1.20, 0.65,
            _m(160), "Offshore wind development and marine installation vessels."),

    # -- Healthcare: binary catalysts, defensive names -----------------------
    Listing("MEDX", "Medex Health", Sector.HEALTHCARE, 94.15, 0.28, 0.09, 0.70, 0.90,
            _m(280), "Hospital networks and outpatient diagnostics."),
    Listing("VITA", "Vitalis Bio", Sector.HEALTHCARE, 57.80, 0.68, 0.18, 1.10, 0.52,
            _m(120), "Three phase-III candidates and a very patient board."),
    Listing("GENO", "Genome Works", Sector.HEALTHCARE, 132.70, 0.55, 0.15, 1.05, 0.70,
            _m(95), "Sequencing instruments and the consumables that print money."),
    Listing("CURA", "Curative Labs", Sector.HEALTHCARE, 28.35, 0.75, 0.20, 1.25, 0.45,
            _m(85), "Cell therapy platform. One readout from glory or ruin."),
    Listing("ORTH", "Orthos Medical", Sector.HEALTHCARE, 71.90, 0.30, 0.08, 0.75, 0.84,
            _m(150), "Implants and surgical robotics with a boring, excellent margin."),
    Listing("PHAR", "Pharos Pharma", Sector.HEALTHCARE, 168.25, 0.26, 0.07, 0.65, 0.93,
            _m(390), "Large-cap pharma; a patent cliff nobody wants to discuss."),

    # -- Finance: low volatility, high beta to the market mood ---------------
    Listing("CAPT", "Capital Trust", Sector.FINANCE, 88.40, 0.27, 0.07, 1.30, 0.91,
            _m(360), "Commercial banking and treasury services."),
    Listing("LEDG", "Ledger Financial", Sector.FINANCE, 45.65, 0.33, 0.09, 1.40, 0.83,
            _m(240), "Clearing, custody and settlement infrastructure."),
    Listing("ARCA", "Arca Bank", Sector.FINANCE, 156.80, 0.24, 0.06, 1.20, 0.94,
            _m(430), "The old money. Survives everything, thrills nobody."),
    Listing("AEGS", "Aegis Insurance", Sector.FINANCE, 103.25, 0.22, 0.06, 0.80, 0.88,
            _m(200), "Property and casualty underwriting with a catastrophe book."),
    Listing("MINT", "Mint Payments", Sector.FINANCE, 62.10, 0.52, 0.17, 1.55, 0.76,
            _m(175), "Payment rails for merchants who hate their incumbent."),
    Listing("STRL", "Sterling Holdings", Sector.FINANCE, 214.60, 0.29, 0.10, 1.15, 0.80,
            _m(90), "Diversified holding company run by a famously terse chairman."),

    # -- Consumer: steady, sentiment-sensitive -------------------------------
    Listing("ORCH", "Orchard Foods", Sector.CONSUMER, 49.30, 0.21, 0.05, 0.55, 0.89,
            _m(300), "Packaged foods and private-label supply."),
    Listing("BREW", "Brew & Co", Sector.CONSUMER, 36.75, 0.25, 0.06, 0.60, 0.85,
            _m(210), "Coffee retail with 11,000 stores and a loyalty app."),
    Listing("LUXE", "Luxe Retail", Sector.CONSUMER, 128.90, 0.44, 0.08, 1.35, 0.72,
            _m(110), "Aspirational goods; the first thing cut in a downturn."),
    Listing("ZEST", "Zest Beverages", Sector.CONSUMER, 58.45, 0.23, 0.07, 0.58, 0.90,
            _m(320), "Soft drinks, sports hydration and one inexplicable energy brand."),
    Listing("HAVN", "Haven Home", Sector.CONSUMER, 82.15, 0.36, 0.06, 1.10, 0.79,
            _m(140), "Home improvement retail, tightly coupled to housing starts."),
    Listing("TREK", "Trek Apparel", Sector.CONSUMER, 41.60, 0.47, 0.09, 1.25, 0.68,
            _m(130), "Technical outdoor clothing with a cult following."),

    # -- Industrial: cyclical, order-book driven -----------------------------
    Listing("AERO", "Aero Dynamics", Sector.INDUSTRIAL, 187.55, 0.35, 0.10, 1.10, 0.84,
            _m(170), "Airframes, propulsion and a very long defence backlog."),
    Listing("FORG", "Forge Industrial", Sector.INDUSTRIAL, 67.20, 0.39, 0.07, 1.30, 0.75,
            _m(155), "Speciality steel and heavy fabrication."),
    Listing("CRAN", "Crane Logistics", Sector.INDUSTRIAL, 95.85, 0.34, 0.08, 1.20, 0.81,
            _m(185), "Port terminals, freight forwarding and cold chain."),
    Listing("TITN", "Titan Machinery", Sector.INDUSTRIAL, 143.40, 0.37, 0.09, 1.25, 0.77,
            _m(120), "Earthmoving equipment sold into mining and construction."),
    Listing("RAIL", "Ironline Rail", Sector.INDUSTRIAL, 209.70, 0.26, 0.07, 0.90, 0.86,
            _m(145), "Freight rail. A toll road that happens to have wheels."),
    Listing("ATLS", "Atlas Robotics", Sector.INDUSTRIAL, 54.95, 0.61, 0.19, 1.60, 0.60,
            _m(105), "Warehouse automation with an aggressive expansion plan."),

    # -- Mining: commodity-linked, violent -----------------------------------
    Listing("TERA", "Terra Minerals", Sector.MINING, 73.15, 0.48, 0.06, 1.30, 0.74,
            _m(200), "Diversified bulk commodities across four jurisdictions."),
    Listing("AURM", "Aurum Gold", Sector.MINING, 112.40, 0.40, 0.05, -0.35, 0.82,
            _m(175), "Gold producer. Tends to zig when the market zags."),
    Listing("LITH", "Lithos Mining", Sector.MINING, 27.60, 0.79, 0.14, 1.65, 0.50,
            _m(115), "Hard-rock lithium; leveraged entirely to battery demand."),
    Listing("IRON", "Ironbark Resources", Sector.MINING, 61.85, 0.53, 0.04, 1.40, 0.70,
            _m(230), "Iron ore at the low end of the cost curve."),
    Listing("COBL", "Cobalt Ridge", Sector.MINING, 18.95, 0.83, 0.11, 1.55, 0.38,
            _m(90), "Single-asset cobalt developer. Permitting risk in human form."),

    # -- Communications: mixed utility and media -----------------------------
    Listing("ECHO", "Echo Networks", Sector.COMMUNICATIONS, 39.85, 0.29, 0.05, 0.85, 0.87,
            _m(370), "Fibre backbone and enterprise connectivity."),
    Listing("WAVE", "Wavelength Telecom", Sector.COMMUNICATIONS, 22.40, 0.31, 0.03, 0.80, 0.83,
            _m(450), "Mobile network operator with a debt pile and a dividend."),
    Listing("ORBT", "Orbital Comms", Sector.COMMUNICATIONS, 96.70, 0.66, 0.21, 1.55, 0.57,
            _m(100), "Low-earth-orbit constellation, currently 40% deployed."),
    Listing("LINQ", "Linq Media", Sector.COMMUNICATIONS, 47.25, 0.50, 0.10, 1.35, 0.71,
            _m(160), "Streaming, sports rights and a content budget with no ceiling."),
    Listing("SGNL", "Signal Broadcasting", Sector.COMMUNICATIONS, 31.55, 0.42, 0.02, 1.05, 0.66,
            _m(140), "Legacy broadcast assets funding a digital pivot."),
)

# fmt: on

SYMBOLS: tuple[str, ...] = tuple(listing.symbol for listing in UNIVERSE)

BY_SYMBOL: dict[str, Listing] = {listing.symbol: listing for listing in UNIVERSE}

SECTORS: tuple[Sector, ...] = tuple(Sector)


def sector_members(sector: Sector) -> tuple[Listing, ...]:
    return tuple(listing for listing in UNIVERSE if listing.sector is sector)
