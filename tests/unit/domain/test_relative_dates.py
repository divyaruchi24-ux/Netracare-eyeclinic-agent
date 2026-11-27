import json
from datetime import UTC, date, datetime, time
from zoneinfo import ZoneInfo

import pytest

from netracare.domain.relative_dates import (
    DateRange,
    TimeWindow,
    explain_unavailable,
    format_day,
    resolve_dates,
    resolve_time,
    utc_bounds,
)
from netracare.evals.date_tasks import relative_dates_task
from tests.conftest import ROOT

CASES = [
    json.loads(line)
    for line in (ROOT / "evals/datasets/relative_dates_v0.jsonl").read_text("utf-8").splitlines()
    if line.strip()
]


@pytest.mark.parametrize("case", CASES, ids=[c["id"] for c in CASES])
def test_dataset(case: dict[str, object]) -> None:
    out = relative_dates_task(case["input"])  # type: ignore[arg-type]
    for key, want in case["expected"].items():  # type: ignore[attr-defined]
        assert out.get(key) == want, (case["input"], out)


def test_labels() -> None:
    r = resolve_dates("next Tuesday", date(2026, 11, 10))
    assert r is not None and r.label == "Tuesday 17 November 2026" and r.days == 1
    span = resolve_dates("next week", date(2026, 11, 10))
    assert span is not None and span.days == 6 and "Nov" in span.label
    assert format_day(date(2026, 12, 1)) == "Tuesday 1 December 2026"


def test_explain_unavailable() -> None:
    today = date(2026, 11, 10)
    holidays = {date(2026, 11, 12): "Clinic holiday"}
    assert "already passed" in (explain_unavailable(date(2026, 11, 9), today, holidays) or "")
    assert "Sundays" in (explain_unavailable(date(2026, 11, 15), today, holidays) or "")
    assert "Clinic holiday" in (explain_unavailable(date(2026, 11, 12), today, holidays) or "")
    assert explain_unavailable(date(2026, 11, 13), today, holidays) is None


def test_no_time_phrase() -> None:
    assert resolve_time("") is None
    assert resolve_time("book me with Dr Rao") is None
    assert resolve_time("on the 12th") is None


@pytest.mark.parametrize(
    ("day", "expected_start_utc"),
    [
        (date(2026, 3, 28), datetime(2026, 3, 28, 9, 0, tzinfo=UTC)),  # GMT (UTC+0)
        (date(2026, 3, 30), datetime(2026, 3, 30, 8, 0, tzinfo=UTC)),  # BST after spring forward
        (date(2026, 10, 24), datetime(2026, 10, 24, 8, 0, tzinfo=UTC)),  # still BST
        (date(2026, 10, 26), datetime(2026, 10, 26, 9, 0, tzinfo=UTC)),  # back to GMT
    ],
)
def test_utc_bounds_across_dst(day: date, expected_start_utc: datetime) -> None:
    """The clinic runs on IST (no DST); the same code is correct in a DST zone too."""
    tz = ZoneInfo("Europe/London")
    start, end = utc_bounds(DateRange(day, day, "x"), None, tz)
    assert start == expected_start_utc
    assert (end - start).total_seconds() == 9 * 3600


def test_utc_bounds_span_crossing_dst_keeps_local_wall_clock() -> None:
    tz = ZoneInfo("Europe/London")
    window = TimeWindow(time(14), time(18), "afternoon")
    start, end = utc_bounds(DateRange(date(2026, 3, 28), date(2026, 3, 30), "x"), window, tz)
    assert start.astimezone(tz).time() == time(14) and end.astimezone(tz).time() == time(18)
    assert start.utcoffset() == end.utcoffset()  # both UTC
    assert start.hour == 14 and end.hour == 17  # +0 before, +1 after the change


def test_ist_has_no_dst() -> None:
    tz = ZoneInfo("Asia/Kolkata")
    start, _ = utc_bounds(DateRange(date(2026, 3, 30), date(2026, 3, 30), "x"), None, tz)
    assert start == datetime(2026, 3, 30, 3, 30, tzinfo=UTC)
