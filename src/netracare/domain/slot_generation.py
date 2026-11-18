"""Generate bookable slots from weekly schedule templates, minus holidays, leaves and lunch."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from netracare.domain.clock import ensure_utc

DEFAULT_LUNCH: tuple[time, time] = (time(13, 0), time(14, 0))


@dataclass(frozen=True)
class WorkBlock:
    """A recurring working window. `weekday`: Monday=0 ... Sunday=6 (clinic-local)."""

    weekday: int
    start: time
    end: time

    def __post_init__(self) -> None:
        if not 0 <= self.weekday <= 6:
            raise ValueError("weekday must be 0..6")
        if self.start >= self.end:
            raise ValueError("block start must be before end")


@dataclass(frozen=True)
class SlotSpec:
    doctor_id: str
    start_utc: datetime
    end_utc: datetime


def _overlaps(a_start: datetime, a_end: datetime, b_start: datetime, b_end: datetime) -> bool:
    return a_start < b_end and b_start < a_end


def generate_slots(
    doctor_id: str,
    blocks: Sequence[WorkBlock],
    leaves: Iterable[tuple[datetime, datetime]],
    holidays: set[date],
    start: date,
    days: int,
    tz: ZoneInfo,
    slot_minutes: int = 15,
    lunch: tuple[time, time] | None = DEFAULT_LUNCH,
    closed_weekdays: frozenset[int] = frozenset({6}),
) -> list[SlotSpec]:
    """Return slots (UTC) for `days` clinic-local days beginning at `start`, sorted by time.

    A slot is skipped when its day is a holiday or closed weekday, when it overlaps lunch,
    or when it overlaps any leave range. Wall-clock times are resolved in `tz`, so DST
    transitions are handled by zoneinfo.
    """
    if slot_minutes <= 0:
        raise ValueError("slot_minutes must be positive")
    step = timedelta(minutes=slot_minutes)
    leave_ranges = [(ensure_utc(s), ensure_utc(e)) for s, e in leaves]
    by_weekday: dict[int, list[WorkBlock]] = {}
    for b in blocks:
        by_weekday.setdefault(b.weekday, []).append(b)

    out: list[SlotSpec] = []
    for offset in range(days):
        day = start + timedelta(days=offset)
        if day in holidays or day.weekday() in closed_weekdays:
            continue
        for block in by_weekday.get(day.weekday(), []):
            cursor = datetime.combine(day, block.start, tzinfo=tz)
            block_end = datetime.combine(day, block.end, tzinfo=tz)
            lunch_range = (
                (
                    datetime.combine(day, lunch[0], tzinfo=tz),
                    datetime.combine(day, lunch[1], tzinfo=tz),
                )
                if lunch
                else None
            )
            while cursor + step <= block_end:
                s_utc = cursor.astimezone(UTC)
                e_utc = (cursor + step).astimezone(UTC)
                in_lunch = lunch_range is not None and _overlaps(
                    cursor, cursor + step, lunch_range[0], lunch_range[1]
                )
                on_leave = any(_overlaps(s_utc, e_utc, ls, le) for ls, le in leave_ranges)
                if not in_lunch and not on_leave:
                    out.append(SlotSpec(doctor_id=doctor_id, start_utc=s_utc, end_utc=e_utc))
                cursor += step
    out.sort(key=lambda s: s.start_utc)
    return out
