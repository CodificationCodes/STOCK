"""Colour palette and the application stylesheet.

The palette is a dark "trading desk" scheme: a near-black background, one
amber accent for chrome, and green/red reserved *exclusively* for price
direction. Nothing else in the UI is allowed to be green or red, so a flash
of colour always means the market moved.
"""

from __future__ import annotations

# -- semantic colours --------------------------------------------------------
UP = "#3ddc84"
DOWN = "#ff5c5c"
NEUTRAL = "#8b93a7"
ACCENT = "#ffb454"
ACCENT_DIM = "#7a5a2a"
TEXT = "#d4d9e4"
TEXT_DIM = "#6b7280"
BACKGROUND = "#0b0e14"
PANEL = "#11151f"
PANEL_ALT = "#161b28"
BORDER = "#232a3a"
BORDER_FOCUS = "#ffb454"
WARNING = "#ffcc66"
INFO = "#59c2ff"

CHART_AXIS = "#5c6470"
CHART_GRID = "#2a3140"

#: Consistent colour per sector, used by the market screen and sector bars.
SECTOR_COLOURS = {
    "Technology": "#59c2ff",
    "Energy": "#ffb454",
    "Healthcare": "#95e6cb",
    "Finance": "#d2a6ff",
    "Consumer": "#f28779",
    "Industrial": "#aad94c",
    "Mining": "#e6b673",
    "Communications": "#73b8ff",
}


def sector_colour(sector: str) -> str:
    return SECTOR_COLOURS.get(sector, NEUTRAL)


#: Textual CSS. Kept in one place so the whole app restyles from here.
APP_CSS = f"""
Screen {{
    background: {BACKGROUND};
    color: {TEXT};
}}

/* ---------------------------------------------------------------- chrome */
#topbar {{
    height: 1;
    background: {PANEL_ALT};
    color: {TEXT};
    padding: 0 1;
}}

#navbar {{
    height: 1;
    background: {PANEL};
    padding: 0 1;
}}

#statusbar {{
    height: 1;
    background: {PANEL_ALT};
    color: {TEXT_DIM};
    padding: 0 1;
}}

#tape {{
    height: 1;
    background: {BACKGROUND};
    color: {NEUTRAL};
    padding: 0 1;
}}

/* ---------------------------------------------------------------- panels */
.panel {{
    background: {PANEL};
    border: round {BORDER};
    padding: 0 1;
}}

.panel:focus-within {{
    border: round {BORDER_FOCUS};
}}

.panel-title {{
    color: {ACCENT};
    text-style: bold;
}}

.muted {{ color: {TEXT_DIM}; }}
.accent {{ color: {ACCENT}; }}
.up {{ color: {UP}; }}
.down {{ color: {DOWN}; }}

/* ------------------------------------------------------------------ auth */
#auth-wrapper {{
    align: center middle;
    width: 100%;
    height: 100%;
}}

#auth-card {{
    width: 60;
    height: auto;
    background: {PANEL};
    border: round {ACCENT};
    padding: 1 2;
}}

#auth-title {{
    text-align: center;
    color: {ACCENT};
    text-style: bold;
    padding-bottom: 1;
}}

#auth-subtitle {{
    text-align: center;
    color: {TEXT_DIM};
    padding-bottom: 1;
}}

#auth-message {{
    height: auto;
    min-height: 2;
    padding-top: 1;
    text-align: center;
}}

#auth-card Input {{
    background: {PANEL_ALT};
    border: tall {BORDER};
    margin-bottom: 1;
}}

#auth-card Input:focus {{
    border: tall {ACCENT};
}}

#auth-buttons {{
    height: auto;
    align-horizontal: center;
}}

Button {{
    background: {PANEL_ALT};
    color: {TEXT};
    border: none;
    height: 3;
    margin: 0 1;
}}

Button:hover {{ background: {BORDER}; }}
Button.-primary {{ background: {ACCENT_DIM}; color: {ACCENT}; text-style: bold; }}
Button.-primary:hover {{ background: {ACCENT}; color: {BACKGROUND}; }}

/* ------------------------------------------------------------ data tables */
DataTable {{
    background: {PANEL};
    color: {TEXT};
    height: 1fr;
}}

DataTable > .datatable--header {{
    background: {PANEL_ALT};
    color: {NEUTRAL};
    text-style: bold;
}}

DataTable > .datatable--cursor {{
    background: {ACCENT_DIM};
    color: {TEXT};
}}

DataTable > .datatable--hover {{
    background: {PANEL_ALT};
}}

/* ---------------------------------------------------------------- layout */
#content {{ height: 1fr; }}

.column {{ height: 1fr; }}

#market-left {{ width: 2fr; }}
#market-right {{ width: 1fr; min-width: 34; }}

#stock-chart {{ height: 1fr; min-height: 12; }}
#stock-header {{ height: auto; padding: 0 1; }}
#stock-timeframes {{ height: auto; padding: 0 1; }}
#stock-lower {{ height: auto; }}
#stock-stats {{ width: 1fr; height: auto; }}
#stock-position {{ width: 1fr; height: auto; }}
#stock-news {{ height: auto; }}

#portfolio-summary {{ height: auto; padding: 0 1; }}

/* ----------------------------------------------------------------- modal */
TradeDialog {{
    align: center middle;
}}

#trade-card {{
    width: 62;
    height: auto;
    background: {PANEL};
    border: round {ACCENT};
    padding: 1 2;
}}

#trade-title {{
    color: {ACCENT};
    text-style: bold;
    padding-bottom: 1;
}}

#trade-preview {{
    height: auto;
    min-height: 5;
    background: {PANEL_ALT};
    padding: 1;
    margin-bottom: 1;
}}

#trade-card Input {{
    background: {PANEL_ALT};
    border: tall {BORDER};
}}

#trade-card Input:focus {{ border: tall {ACCENT}; }}

#trade-error {{ height: auto; color: {DOWN}; }}

HelpDialog {{ align: center middle; }}

#help-card {{
    width: 76;
    height: auto;
    max-height: 90%;
    background: {PANEL};
    border: round {ACCENT};
    padding: 1 2;
}}

ConfirmDialog {{ align: center middle; }}

#confirm-card {{
    width: 54;
    height: auto;
    background: {PANEL};
    border: round {ACCENT};
    padding: 1 2;
}}

InsiderDialog {{ align: center middle; }}

#insider-card {{
    width: 64;
    height: auto;
    max-height: 90%;
    background: {PANEL};
    border: round {WARNING};
    padding: 1 2;
}}

PlayerDialog {{ align: center middle; }}

#player-card {{
    width: 66;
    height: auto;
    max-height: 90%;
    background: {PANEL};
    border: round {ACCENT};
    padding: 1 2;
}}

/* ------------------------------------------------------------------ misc */
Tooltip {{
    background: {PANEL_ALT};
    color: {TEXT};
    border: round {BORDER};
}}

Toast {{
    background: {PANEL_ALT};
    border: round {BORDER};
}}

LoadingIndicator {{
    background: {BACKGROUND};
    color: {ACCENT};
}}
"""
