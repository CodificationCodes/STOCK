"""Wall-clock trading hours.

The simulation itself has no notion of real time -- it counts ticks. This
module is the one place that answers "is the market open right now", so the
engine can freeze outside the session and nothing else has to care.

Times are interpreted in ``market_timezone``, so a schedule written as 09:00
follows the local clock across daylight saving rather than drifting an hour
twice a year.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

SATURDAY = 5


def parse_hhmm(value: str) -> time:
    """Parse ``"09:00"`` into a :class:`~datetime.time`."""
    try:
        hour, minute = (int(part) for part in value.strip().split(":", 1))
        return time(hour=hour, minute=minute)
    except (ValueError, TypeError) as exc:
        raise ValueError(f"expected a HH:MM time, got {value!r}") from exc


@dataclass(frozen=True, slots=True)
class TradingHours:
    """A daily open/close window in a fixed timezone."""

    zone: ZoneInfo
    opens: time
    closes: time
    weekdays_only: bool

    @classmethod
    def from_settings(cls, settings) -> TradingHours:
        opens = parse_hhmm(settings.market_open_time)
        closes = parse_hhmm(settings.market_close_time)
        if opens >= closes:
            raise ValueError(
                f"market_open_time ({opens:%H:%M}) must be before "
                f"market_close_time ({closes:%H:%M})"
            )
        return cls(
            zone=ZoneInfo(settings.market_timezone),
            opens=opens,
            closes=closes,
            weekdays_only=settings.market_weekdays_only,
        )

    def is_open(self, now: datetime) -> bool:
        local = now.astimezone(self.zone)
        if self.weekdays_only and local.weekday() >= SATURDAY:
            return False
        return self.opens <= local.time() < self.closes

    def next_open(self, now: datetime) -> datetime:
        """The next moment the market opens, as UTC. Never returns ``now``."""
        local = now.astimezone(self.zone)
        candidate = local.replace(
            hour=self.opens.hour, minute=self.opens.minute, second=0, microsecond=0
        )
        if local.time() >= self.opens:
            candidate += timedelta(days=1)
        while self.weekdays_only and candidate.weekday() >= SATURDAY:
            candidate += timedelta(days=1)
        return candidate.astimezone(timezone.utc)

    def describe(self) -> str:
        days = "Mon-Fri" if self.weekdays_only else "daily"
        return f"{self.opens:%H:%M}-{self.closes:%H:%M} {self.zone.key} ({days})"
