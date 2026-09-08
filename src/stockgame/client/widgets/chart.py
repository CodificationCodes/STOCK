"""Terminal price charts.

Two renderers, both pure text so they work in any terminal that can show
box-drawing characters -- no images, no sixel, no special font:

**Candlesticks** use one column per bar. Each cell is chosen from a small
alphabet (``│`` wick, ``█`` full body, ``▀``/``▄`` half bodies) so a body can
start and end mid-cell, which doubles the effective vertical resolution.

**Line** mode uses Braille (U+2800..) at 2x4 sub-cell resolution, giving
roughly eight times the detail of a block plot in the same space. It falls
back automatically when a terminal cannot encode them.

Both are :class:`~rich.console.ConsoleRenderable`, so Rich handles the
measuring and Textual just puts them in a box.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from rich.console import Console, ConsoleOptions, RenderResult
from rich.segment import Segment
from rich.style import Style
from rich.text import Text

from stockgame.client.theme import (
    CHART_AXIS,
    CHART_GRID,
    DOWN,
    NEUTRAL,
    UP,
)
from stockgame.shared.money import fmt_money

#: Braille dot bit positions, indexed [column][row].
BRAILLE_DOTS = ((0x01, 0x02, 0x04, 0x40), (0x08, 0x10, 0x20, 0x80))
PRICE_GUTTER = 10


@dataclass(slots=True)
class Bar:
    open: int
    high: int
    low: int
    close: int
    volume: int = 0

    @classmethod
    def from_payload(cls, raw: dict[str, Any]) -> Bar:
        return cls(
            open=int(raw.get("o", 0)),
            high=int(raw.get("h", 0)),
            low=int(raw.get("l", 0)),
            close=int(raw.get("c", 0)),
            volume=int(raw.get("v", 0)),
        )


class PriceChart:
    """Renderable OHLC chart with an optional volume strip."""

    def __init__(
        self,
        bars: list[Bar],
        *,
        style: str = "candles",
        show_volume: bool = True,
        show_axis: bool = True,
        reference: int | None = None,
        height: int | None = None,
    ) -> None:
        self.bars = bars
        self.style = style
        self.show_volume = show_volume
        self.show_axis = show_axis
        #: Previous close; drawn as a dashed line so intraday moves have context.
        self.reference = reference
        self.height = height

    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RenderResult:
        width = max(20, options.max_width)
        height = self.height or max(8, min(options.height or 20, 40))

        if not self.bars:
            yield Text("  no price history yet", style=CHART_AXIS)
            return

        gutter = PRICE_GUTTER if self.show_axis else 0
        plot_width = max(8, width - gutter)
        volume_height = 3 if self.show_volume and height >= 12 else 0
        plot_height = max(4, height - volume_height)

        bars = self.bars[-plot_width:] if self.style == "candles" else self.bars
        low, high = self._bounds(bars)

        rows = (
            self._render_candles(bars, plot_width, plot_height, low, high)
            if self.style == "candles"
            else self._render_line(bars, plot_width, plot_height, low, high)
        )

        labels = (
            self._price_labels(plot_height, low, high) if self.show_axis else [""] * plot_height
        )
        for index, row in enumerate(rows):
            if self.show_axis:
                yield Segment(f"{labels[index]:>{gutter - 1}} ", Style.parse(CHART_AXIS))
            yield from row
            yield Segment("\n")

        if volume_height:
            yield from self._render_volume(bars, plot_width, volume_height, gutter)

    # -- scaling ------------------------------------------------------------

    def _bounds(self, bars: list[Bar]) -> tuple[float, float]:
        low = min(bar.low for bar in bars)
        high = max(bar.high for bar in bars)
        if self.reference is not None:
            low = min(low, self.reference)
            high = max(high, self.reference)
        if high <= low:
            # A perfectly flat series still needs a non-zero range to divide by.
            pad = max(1.0, high * 0.01)
            return low - pad, high + pad
        pad = (high - low) * 0.06
        return low - pad, high + pad

    def _price_labels(self, height: int, low: float, high: float) -> list[str]:
        labels = []
        for row in range(height):
            # Label every other row; a label on every line is unreadable.
            if row % 2 or row == height - 1:
                value = high - (high - low) * (row / max(1, height - 1))
                labels.append(fmt_money(int(value), compact=value >= 1_000_000))
            else:
                labels.append("")
        return labels

    # -- candles ------------------------------------------------------------

    def _render_candles(
        self, bars: list[Bar], width: int, height: int, low: float, high: float
    ) -> list[list[Segment]]:
        span = high - low

        # Two sub-rows per cell, so bodies can begin or end on a half-line.
        def to_sub(value: float) -> float:
            return (high - value) / span * (height * 2 - 1)

        grid: list[list[Segment]] = [[] for _ in range(height)]
        reference_row = int(to_sub(self.reference) // 2) if self.reference is not None else None

        # When there are fewer bars than columns (a 3M chart on a wide
        # terminal) each candle is widened to fill the space, rather than
        # leaving two thirds of the panel blank.
        cell_width = max(1, len(bars) and self._cell_width(len(bars), width))
        columns = []
        for bar in bars:
            rising = bar.close >= bar.open
            colour = UP if rising else DOWN
            body_top = to_sub(max(bar.open, bar.close))
            body_bottom = to_sub(min(bar.open, bar.close))
            wick_top = to_sub(bar.high)
            wick_bottom = to_sub(bar.low)
            columns.extend([(colour, body_top, body_bottom, wick_top, wick_bottom)] * cell_width)

        pad = width - len(columns)
        for row in range(height):
            if pad > 0:
                grid[row].append(Segment(" " * pad))
            top_sub, bottom_sub = row * 2, row * 2 + 1
            for colour, body_top, body_bottom, wick_top, wick_bottom in columns:
                char = " "
                style = colour
                upper_body = body_top <= top_sub <= body_bottom
                lower_body = body_top <= bottom_sub <= body_bottom
                if upper_body and lower_body:
                    char = "█"
                elif upper_body:
                    char = "▀"
                elif lower_body:
                    char = "▄"
                elif wick_top <= top_sub <= wick_bottom or wick_top <= bottom_sub <= wick_bottom:
                    char = "│"
                elif reference_row is not None and row == reference_row:
                    char, style = "╌", CHART_GRID
                grid[row].append(Segment(char, Style.parse(style)))
        return grid

    # -- line ---------------------------------------------------------------

    @staticmethod
    def _cell_width(bar_count: int, width: int) -> int:
        """Columns per candle, capped so wide bars never look like blocks."""
        return max(1, min(4, width // max(1, bar_count)))

    def _render_line(
        self, bars: list[Bar], width: int, height: int, low: float, high: float
    ) -> list[list[Segment]]:
        span = high - low
        cells_x, cells_y = width * 2, height * 4
        canvas = [[0] * width for _ in range(height)]

        points: list[tuple[int, int]] = []
        for index, bar in enumerate(bars):
            x = int(index / max(1, len(bars) - 1) * (cells_x - 1)) if len(bars) > 1 else 0
            y = int((high - bar.close) / span * (cells_y - 1))
            points.append((x, max(0, min(cells_y - 1, y))))

        for index in range(len(points) - 1):
            self._plot_line(canvas, points[index], points[index + 1], width, height)
        if len(points) == 1:
            self._set_dot(canvas, points[0][0], points[0][1], width, height)

        rising = bars[-1].close >= bars[0].open
        colour = Style.parse(UP if rising else DOWN)
        grid_style = Style.parse(CHART_GRID)
        reference_row = (
            int((high - self.reference) / span * (height - 1))
            if self.reference is not None
            else None
        )

        rows: list[list[Segment]] = []
        for y in range(height):
            segments: list[Segment] = []
            for x in range(width):
                bits = canvas[y][x]
                if bits:
                    segments.append(Segment(chr(0x2800 + bits), colour))
                elif reference_row is not None and y == reference_row:
                    segments.append(Segment("╌", grid_style))
                else:
                    segments.append(Segment(" "))
            rows.append(segments)
        return rows

    def _set_dot(self, canvas, x: int, y: int, width: int, height: int) -> None:
        col, row = x // 2, y // 4
        if 0 <= row < height and 0 <= col < width:
            canvas[row][col] |= BRAILLE_DOTS[x % 2][y % 4]

    def _plot_line(self, canvas, start, end, width: int, height: int) -> None:
        """Bresenham between two sub-cell points."""
        x0, y0 = start
        x1, y1 = end
        dx, dy = abs(x1 - x0), -abs(y1 - y0)
        sx = 1 if x0 < x1 else -1
        sy = 1 if y0 < y1 else -1
        error = dx + dy
        while True:
            self._set_dot(canvas, x0, y0, width, height)
            if x0 == x1 and y0 == y1:
                return
            doubled = 2 * error
            if doubled >= dy:
                error += dy
                x0 += sx
            if doubled <= dx:
                error += dx
                y0 += sy

    # -- volume -------------------------------------------------------------

    def _render_volume(self, bars: list[Bar], width: int, height: int, gutter: int) -> RenderResult:
        peak = max((bar.volume for bar in bars), default=0)
        blocks = " ▁▂▃▄▅▆▇█"
        yield Segment(" " * gutter)
        yield Segment("─" * width, Style.parse(CHART_GRID))
        yield Segment("\n")
        if not peak:
            return
        cell_width = self._cell_width(len(bars), width) if self.style == "candles" else 1
        pad = width - len(bars) * cell_width
        yield Segment(f"{'VOL':>{max(0, gutter - 1)}} ", Style.parse(CHART_AXIS))
        if pad > 0:
            yield Segment(" " * pad)
        for bar in bars:
            level = int(bar.volume / peak * (len(blocks) - 1))
            colour = UP if bar.close >= bar.open else DOWN
            yield Segment(blocks[level] * cell_width, Style.parse(f"dim {colour}"))
        yield Segment("\n")


def sparkline(values: list[float], width: int = 12, *, positive: bool | None = None) -> Text:
    """A compact inline trend indicator, always exactly ``width`` cells.

    Short series are left-padded so the newest point sits on the right edge,
    matching the charts -- and so the column stays aligned in a table.
    """
    if not values:
        return Text(" " * width)
    blocks = "▁▂▃▄▅▆▇█"
    sampled = _resample(values, width)
    low, high = min(sampled), max(sampled)
    span = (high - low) or 1.0
    if positive is None:
        positive = sampled[-1] >= sampled[0]
    style = UP if positive else DOWN
    text = Text(style=style)
    if len(sampled) < width:
        text.append(" " * (width - len(sampled)))
    for value in sampled:
        index = int((value - low) / span * (len(blocks) - 1))
        text.append(blocks[max(0, min(len(blocks) - 1, index))])
    return text


def _resample(values: list[float], width: int) -> list[float]:
    if len(values) <= width:
        return values
    step = len(values) / width
    return [values[min(len(values) - 1, int(index * step))] for index in range(width)]


def format_change(change_pct: float, *, arrow: bool = True) -> Text:
    """``+2.40%`` in green with an up arrow, or the red equivalent."""
    if change_pct > 0.0005:
        symbol, style = "▲", UP
    elif change_pct < -0.0005:
        symbol, style = "▼", DOWN
    else:
        symbol, style = "•", NEUTRAL
    body = f"{symbol} {change_pct:+.2f}%" if arrow else f"{change_pct:+.2f}%"
    return Text(body, style=style)


def heat_style(change_pct: float) -> str:
    """Colour ramp used by the market grid, so a glance reads as a heatmap."""
    if change_pct >= 3:
        return "bold #00e07a"
    if change_pct >= 1:
        return "#3ddc84"
    if change_pct > 0:
        return "#87d7a0"
    if change_pct == 0:
        return NEUTRAL
    if change_pct > -1:
        return "#e8a0a0"
    if change_pct > -3:
        return "#ff6b6b"
    return "bold #ff3b3b"


def scale_bar(fraction: float, width: int = 10) -> str:
    """Horizontal fill bar, used for portfolio weights and day ranges."""
    fraction = max(0.0, min(1.0, fraction))
    filled = fraction * width
    whole = int(filled)
    remainder = filled - whole
    partials = " ▏▎▍▌▋▊▉█"
    tail = partials[int(remainder * 8)] if whole < width else ""
    return ("█" * whole + tail).ljust(width)


def day_range_marker(low: int, high: int, price: int, width: int = 16) -> Text:
    """``├────●───┤`` showing where the price sits in the day's range."""
    if high <= low:
        return Text("─" * width, style=CHART_GRID)
    position = int((price - low) / (high - low) * (width - 1))
    position = max(0, min(width - 1, position))
    text = Text()
    text.append("├", style=CHART_GRID)
    text.append("─" * position, style=CHART_GRID)
    text.append("●", style="bold white")
    text.append("─" * (width - 1 - position), style=CHART_GRID)
    text.append("┤", style=CHART_GRID)
    return text


def safe_ratio(numerator: float, denominator: float) -> float:
    return numerator / denominator if denominator else 0.0


def log_scale(value: float) -> float:  # pragma: no cover - reserved for log charts
    return math.log10(value) if value > 0 else 0.0
