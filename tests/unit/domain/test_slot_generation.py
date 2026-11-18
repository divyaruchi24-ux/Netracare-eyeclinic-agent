from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from netracare.domain.slot_generation import WorkBlock, generate_slots

IST = ZoneInfo("Asia/Kolkata")
MON = date(2026, 11, 9)  # a Monday
ALL_WEEK = [WorkBlock(d, time(9), time(18)) for d in range(7)]


def _gen(**kw: object) -> list:  # type: ignore[type-arg]
    args: dict[str, object] = {
        "doctor_id": "d1",
        "blocks": ALL_WEEK,
        "leaves": [],
        "holidays": set(),
        "start": MON,
        "days": 7,
        "tz": IST,
    }
    args.update(kw)
    return generate_slots(**args)  # type: ignore[arg-type]


def _local_dates(slots: list) -> set[date]:  # type: ignore[type-arg]
    return {s.start_utc.astimezone(IST).date() for s in slots}


def test_per_day_count_excludes_lunch() -> None:
    slots = _gen(days=1)
    # 9h window minus 1h lunch = 8h = 32 x 15min
    assert len(slots) == 32


def test_skips_sunday() -> None:
    assert MON + timedelta(days=6) not in _local_dates(_gen())


def test_skips_holiday() -> None:
    hol = MON + timedelta(days=2)
    assert hol not in _local_dates(_gen(holidays={hol}))


def test_skips_lunch() -> None:
    for s in _gen(days=1):
        local = s.start_utc.astimezone(IST).time()
        assert not (time(13) <= local < time(14))


def test_respects_leave_partial_overlap() -> None:
    leave_start = datetime(2026, 11, 9, 10, 5, tzinfo=IST)
    leave_end = datetime(2026, 11, 9, 11, 0, tzinfo=IST)
    slots = _gen(days=1, leaves=[(leave_start, leave_end)])
    local_starts = {s.start_utc.astimezone(IST).time() for s in slots}
    assert time(10, 0) not in local_starts  # partially overlapped
    assert time(10, 45) not in local_starts
    assert time(9, 45) in local_starts
    assert time(11, 0) in local_starts


def test_slots_are_utc_and_15_min() -> None:
    for s in _gen(days=1):
        assert s.start_utc.tzinfo is UTC
        assert s.end_utc - s.start_utc == timedelta(minutes=15)
    first = _gen(days=1)[0]
    assert first.start_utc == datetime(2026, 11, 9, 3, 30, tzinfo=UTC)  # 09:00 IST


def test_deterministic_and_sorted() -> None:
    a, b = _gen(), _gen()
    assert a == b
    assert a == sorted(a, key=lambda s: s.start_utc)


def test_only_listed_weekdays() -> None:
    slots = _gen(blocks=[WorkBlock(2, time(9), time(12))])
    assert _local_dates(slots) == {MON + timedelta(days=2)}
    assert len(slots) == 12


def test_non_utc_dst_zone_correct() -> None:
    ny = ZoneInfo("America/New_York")
    # DST ends Sun 2026-11-01; generate Fri Oct 30 and Mon Nov 2 (Sunday closed)
    slots = generate_slots("d1", ALL_WEEK, [], set(), date(2026, 10, 30), 4, ny, lunch=None)
    by_day: dict[date, datetime] = {}
    for s in slots:
        by_day.setdefault(s.start_utc.astimezone(ny).date(), s.start_utc)
    assert by_day[date(2026, 10, 30)].hour == 13  # 09:00 EDT = 13:00 UTC
    assert by_day[date(2026, 11, 2)].hour == 14  # 09:00 EST = 14:00 UTC


def test_invalid_inputs() -> None:
    with pytest.raises(ValueError):
        WorkBlock(7, time(9), time(10))
    with pytest.raises(ValueError):
        WorkBlock(0, time(10), time(9))
    with pytest.raises(ValueError):
        _gen(slot_minutes=0)
