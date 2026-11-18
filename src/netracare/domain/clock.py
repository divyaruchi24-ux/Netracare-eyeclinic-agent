"""Injectable clock so time-dependent logic is testable."""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Protocol
from zoneinfo import ZoneInfo


class Clock(Protocol):
    def now_utc(self) -> datetime: ...


class SystemClock:
    def now_utc(self) -> datetime:
        return datetime.now(UTC)


class FixedClock:
    def __init__(self, at: datetime) -> None:
        if at.tzinfo is None:
            raise ValueError("FixedClock requires a timezone-aware datetime")
        self._at = at.astimezone(UTC)

    def now_utc(self) -> datetime:
        return self._at


def ensure_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        raise ValueError("naive datetime not allowed; attach a timezone")
    return dt.astimezone(UTC)


def to_clinic_local(dt: datetime, tz: ZoneInfo) -> datetime:
    return ensure_utc(dt).astimezone(tz)


def clinic_today(clock: Clock, tz: ZoneInfo) -> date:
    return to_clinic_local(clock.now_utc(), tz).date()
