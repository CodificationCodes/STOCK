"""Wall-clock trading hours.

These are pure-function tests with an explicit clock, so they behave the same
whatever time the suite actually runs at.
"""

from __future__ import annotations

from datetime import datetime, time, timezone
from zoneinfo import ZoneInfo

import pytest

from stockgame.server.market.hours import TradingHours, parse_hhmm
from stockgame.shared.enums import MarketStatus

SYDNEY = ZoneInfo("Australia/Sydney")
UTC = timezone.utc
UTC_ZONE = ZoneInfo("UTC")


def _window_excluding_now() -> TradingHours:
    """Whichever half of the UTC day does not contain "now"."""
    if datetime.now(UTC).time() < time(12, 0):
        opens, closes = time(12, 0), time(23, 59)
    else:
        opens, closes = time(0, 0), time(11, 59)
    return TradingHours(zone=UTC_ZONE, opens=opens, closes=closes, weekdays_only=False)


def _window_including_now() -> TradingHours:
    return TradingHours(zone=UTC_ZONE, opens=time(0, 0), closes=time(23, 59), weekdays_only=False)


def sydney_hours(weekdays_only: bool = True) -> TradingHours:
    return TradingHours(
        zone=SYDNEY, opens=time(9, 0), closes=time(15, 0), weekdays_only=weekdays_only
    )


def at(year, month, day, hour, minute=0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=SYDNEY)


class TestParsing:
    def test_parses_hhmm(self):
        assert parse_hhmm("09:00") == time(9, 0)
        assert parse_hhmm(" 15:30 ") == time(15, 30)

    @pytest.mark.parametrize("raw", ["", "9", "nine:00", "09:xx"])
    def test_rejects_junk(self, raw):
        with pytest.raises(ValueError):
            parse_hhmm(raw)


class TestIsOpen:
    @pytest.mark.parametrize(
        ("label", "moment", "expected"),
        [
            ("just before the bell", at(2026, 9, 7, 8, 59), False),
            ("on the bell", at(2026, 9, 7, 9, 0), True),
            ("mid session", at(2026, 9, 7, 12, 0), True),
            ("last minute", at(2026, 9, 7, 14, 59), True),
            ("on the close", at(2026, 9, 7, 15, 0), False),
            ("after hours", at(2026, 9, 7, 22, 0), False),
        ],
    )
    def test_session_boundaries(self, label, moment, expected):
        assert sydney_hours().is_open(moment) is expected, label

    def test_weekends_are_closed(self):
        hours = sydney_hours()
        assert hours.is_open(at(2026, 9, 12, 12)) is False  # Saturday
        assert hours.is_open(at(2026, 9, 13, 12)) is False  # Sunday

    def test_weekends_open_when_not_restricted(self):
        hours = sydney_hours(weekdays_only=False)
        assert hours.is_open(at(2026, 9, 12, 12)) is True

    def test_uses_sydney_not_the_host_clock(self):
        # 00:30 UTC is 10:30 in Sydney: open, despite being the small hours
        # anywhere the server is likely to be hosted.
        moment = datetime(2026, 9, 7, 0, 30, tzinfo=ZoneInfo("UTC"))
        assert sydney_hours().is_open(moment) is True


class TestNextOpen:
    def test_later_today_when_before_the_bell(self):
        nxt = sydney_hours().next_open(at(2026, 9, 7, 6, 0))
        assert nxt.astimezone(SYDNEY) == at(2026, 9, 7, 9, 0)

    def test_tomorrow_when_the_session_has_started(self):
        nxt = sydney_hours().next_open(at(2026, 9, 7, 12, 0))
        assert nxt.astimezone(SYDNEY) == at(2026, 9, 8, 9, 0)

    def test_skips_the_weekend(self):
        nxt = sydney_hours().next_open(at(2026, 9, 11, 16, 0))  # Friday, after close
        assert nxt.astimezone(SYDNEY) == at(2026, 9, 14, 9, 0)  # Monday

    def test_holds_local_time_across_daylight_saving(self):
        # Sydney moves to AEDT on 4 October 2026. The bell must stay at 09:00
        # local, which means its UTC offset shifts by an hour.
        before = sydney_hours().next_open(at(2026, 10, 1, 16, 0))
        after = sydney_hours().next_open(at(2026, 10, 6, 16, 0))
        assert before.astimezone(SYDNEY).hour == 9
        assert after.astimezone(SYDNEY).hour == 9
        # Same local bell, an hour apart in UTC: 23:00 under AEST, 22:00 AEDT.
        assert before.hour == 23
        assert after.hour == 22


class TestEngineFollowsTheSchedule:
    """The engine reads the wall clock, so these drive it through the clock."""

    @pytest.mark.asyncio
    async def test_closed_outside_the_session_freezes_prices(self, game):
        engine = game.engine
        # A window that cannot contain "now", whenever the suite runs.
        engine.hours = _window_excluding_now()
        before = engine.prices()

        result = await engine.tick()

        assert engine.status is MarketStatus.CLOSED
        assert result.status_changed is MarketStatus.CLOSED
        assert result.prices == []
        assert engine.prices() == before

    @pytest.mark.asyncio
    async def test_closing_leaves_prices_and_the_day_alone(self, game):
        """The overnight gap must not land on a frozen screen at the close."""
        engine = game.engine
        day_before = engine.day_index
        engine.hours = _window_excluding_now()

        result = await engine.tick()

        assert result.status_changed is MarketStatus.CLOSED
        assert result.day_rolled is None
        assert engine.day_index == day_before

    @pytest.mark.asyncio
    async def test_reopening_closes_out_the_interrupted_day_exactly_once(self, game):
        """Friday's last hour ends at Monday's open -- with the gap, the
        snapshot and a fresh 'today' -- and then does not roll again."""
        engine = game.engine
        engine.hours = _window_excluding_now()
        await engine.tick()
        day_at_close = engine.day_index
        engine.day_started_at = datetime(2020, 1, 1, tzinfo=UTC)  # the weekend
        engine.hours = _window_including_now()

        reopened = await engine.tick()
        following = await engine.tick()

        assert reopened.status_changed is MarketStatus.OPEN
        assert reopened.day_rolled == day_at_close + 1
        assert engine.day_index == day_at_close + 1
        assert following.day_rolled is None

    @pytest.mark.asyncio
    async def test_a_halt_outranks_the_schedule(self, game):
        engine = game.engine
        engine.set_status(MarketStatus.HALTED)
        engine.hours = _window_including_now()

        await engine.tick()

        assert engine.status is MarketStatus.HALTED


class TestFromSettings:
    def test_reads_the_settings(self, settings):
        settings.market_timezone = "Australia/Sydney"
        settings.market_open_time = "09:00"
        settings.market_close_time = "15:00"
        settings.market_weekdays_only = True
        hours = TradingHours.from_settings(settings)
        assert hours.describe() == "09:00-15:00 Australia/Sydney (Mon-Fri)"

    def test_rejects_a_close_before_the_open(self, settings):
        settings.market_open_time = "15:00"
        settings.market_close_time = "09:00"
        with pytest.raises(ValueError, match="must be before"):
            TradingHours.from_settings(settings)
