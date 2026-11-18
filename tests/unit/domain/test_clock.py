from datetime import UTC, date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from netracare.domain.clock import FixedClock, SystemClock, clinic_today, ensure_utc

IST = ZoneInfo("Asia/Kolkata")


def test_fixed_clock_normalises_to_utc() -> None:
    clock = FixedClock(datetime(2026, 11, 10, 10, 0, tzinfo=IST))
    assert clock.now_utc() == datetime(2026, 11, 10, 4, 30, tzinfo=UTC)


def test_fixed_clock_rejects_naive() -> None:
    with pytest.raises(ValueError):
        FixedClock(datetime(2026, 11, 10))


def test_system_clock_is_aware() -> None:
    assert SystemClock().now_utc().tzinfo is not None


def test_clinic_today_near_midnight_utc() -> None:
    # 20:00 UTC on Nov 9 is 01:30 IST on Nov 10
    clock = FixedClock(datetime(2026, 11, 9, 20, 0, tzinfo=UTC))
    assert clinic_today(clock, IST) == date(2026, 11, 10)


def test_ensure_utc_converts_offsets() -> None:
    dt = datetime(2026, 1, 1, 12, 0, tzinfo=timezone(timedelta(hours=5, minutes=30)))
    assert ensure_utc(dt).hour == 6
