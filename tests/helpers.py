"""Shared test helpers."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta


class MutableClock:
    """A controllable clock for tests (FixedClock is immutable)."""

    def __init__(self, at: datetime) -> None:
        if at.tzinfo is None:
            raise ValueError("MutableClock requires an aware datetime")
        self._at = at.astimezone(UTC)

    def now_utc(self) -> datetime:
        return self._at

    def set(self, at: datetime) -> None:
        self._at = at.astimezone(UTC)

    def advance(self, delta: timedelta) -> None:
        self._at += delta
