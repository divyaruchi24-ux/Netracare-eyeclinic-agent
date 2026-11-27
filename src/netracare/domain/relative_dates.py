"""Resolve date and time phrases ("next Tuesday morning") to clinic-local ranges - in code.

The booking agent's LLM only copies the phrase the patient wrote; this module decides what it
means, anchored to the clinic-local "today" from the injected `Clock`, so results are testable
and the model can never invent a date. The read-back always shows the full date, which is how
the patient catches an interpretation they did not mean.

Weekday rules (documented in docs/phase-4/understanding.md 3.7):
- "Friday", "this Friday", "on Friday": the next Friday, today included;
- "next Friday": the Friday of next calendar week (weeks start on Monday);
- "this weekend": the coming Saturday (Sundays are closed), "next weekend": next week's.
Numeric dates are day-first ("12/12", "3/1" = 3 January), as used in India.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
_WD_ALIASES = {
    "mon": 0, "tue": 1, "tues": 1, "wed": 2, "weds": 2, "thu": 3, "thur": 3, "thurs": 3,
    "fri": 4, "sat": 5, "sun": 6,
    **{name: i for i, name in enumerate(WEEKDAYS)},
}  # fmt: skip
MONTHS = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3, "apr": 4,
    "april": 4, "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7, "aug": 8, "august": 8,
    "sep": 9, "sept": 9, "september": 9, "oct": 10, "october": 10, "nov": 11, "november": 11,
    "dec": 12, "december": 12,
}  # fmt: skip
_NUMBER_WORDS = {
    "a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10,
}  # fmt: skip

OPEN = time(9, 0)
CLOSE = time(18, 0)
_WD = "|".join(sorted(_WD_ALIASES, key=len, reverse=True))
_MON = "|".join(sorted(MONTHS, key=len, reverse=True))
_NUM = r"(\d{1,2}|" + "|".join(_NUMBER_WORDS) + r")"


@dataclass(frozen=True)
class DateRange:
    start: date
    end: date
    label: str

    @property
    def days(self) -> int:
        return (self.end - self.start).days + 1


@dataclass(frozen=True)
class TimeWindow:
    start: time
    end: time
    label: str
    out_of_hours: bool = False


def format_day(d: date) -> str:
    """'Tuesday 1 December 2026'."""
    return f"{WEEKDAYS[d.weekday()].capitalize()} {d.day} {d.strftime('%B')} {d.year}"


def _single(d: date) -> DateRange:
    return DateRange(d, d, format_day(d))


def _span(a: date, b: date) -> DateRange:
    if a == b:
        return _single(a)
    return DateRange(a, b, f"{a.day} {a.strftime('%b')} - {b.day} {b.strftime('%b %Y')}")


def _norm(text: str) -> str:
    t = text.lower().replace("’", "'")
    t = re.sub(r"(\d)(st|nd|rd|th)\b", r"\1", t)
    return re.sub(r"\s+", " ", t).strip()


def _next_weekday(today: date, wd: int) -> date:
    return today + timedelta(days=(wd - today.weekday()) % 7)


def _monday(d: date) -> date:
    return d - timedelta(days=d.weekday())


def _number(token: str) -> int:
    return int(token) if token.isdigit() else _NUMBER_WORDS[token]


def _safe_date(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _future_month_day(today: date, month: int, day: int) -> date | None:
    """`day month` this year, or next year if that date has already passed."""
    d = _safe_date(today.year, month, day)
    if d is not None and d < today:
        d = _safe_date(today.year + 1, month, day)
    return d


def resolve_dates(phrase: str, today: date, *, search_days: int = 14) -> DateRange | None:
    """The date range a phrase refers to, or None when it names no date.

    A past explicit date ("1 Jan 2020") is returned as is; the caller explains it.
    """
    t = _norm(phrase)
    if not t:
        return None
    if re.search(r"\b(asap|as soon as possible|earliest|soonest|any ?day|whenever)\b", t):
        return _span(today, today + timedelta(days=search_days - 1))
    if re.search(r"\b(day after tomorrow|overmorrow|parso)\b", t):
        return _single(today + timedelta(days=2))
    if re.search(r"\b(tomorrow|tmrw|tmr|kal)\b", t):
        return _single(today + timedelta(days=1))
    if re.search(r"\b(today|tonight|aaj)\b", t):
        return _single(today)

    m = re.search(r"\bin " + _NUM + r" (day|days|week|weeks)\b", t)
    if m:
        n = _number(m.group(1)) * (7 if m.group(2).startswith("week") else 1)
        return _single(today + timedelta(days=n))

    if re.search(r"\bnext weekend\b", t):
        return _single(_monday(today) + timedelta(days=7 + 5))
    if re.search(r"\b(this )?weekend\b", t):
        sat = _next_weekday(today, 5)
        return _single(sat)
    if re.search(r"\bnext week\b", t):
        mon = _monday(today) + timedelta(days=7)
        return _span(mon, mon + timedelta(days=5))
    if re.search(r"\bthis week\b", t):
        sat = _monday(today) + timedelta(days=5)
        return _span(today, max(today, sat))
    if re.search(r"\bthis month\b", t):
        nxt = date(today.year + (today.month == 12), today.month % 12 + 1, 1)
        return _span(today, nxt - timedelta(days=1))

    m = re.search(r"\bnext (" + _WD + r")\b", t)
    if m:
        wd = _WD_ALIASES[m.group(1)]
        return _single(_monday(today) + timedelta(days=7 + wd))
    m = re.search(r"\b(?:this |on |coming )?(" + _WD + r")\b", t)
    if m:
        return _single(_next_weekday(today, _WD_ALIASES[m.group(1)]))

    # "12 dec 2026", "12 december", "dec 12"
    m = re.search(r"\b(\d{1,2}) (?:of )?(" + _MON + r")\b(?: (\d{4}))?", t) or None
    if m:
        day, month, year = int(m.group(1)), MONTHS[m.group(2)], m.group(3)
        d = _safe_date(int(year), month, day) if year else _future_month_day(today, month, day)
        return _single(d) if d else None
    m = re.search(r"\b(" + _MON + r") (\d{1,2})\b(?: (\d{4}))?", t)
    if m:
        month, day, year = MONTHS[m.group(1)], int(m.group(2)), m.group(3)
        d = _safe_date(int(year), month, day) if year else _future_month_day(today, month, day)
        return _single(d) if d else None
    # "12/12", "12-12-2026", "3/1" (day first)
    m = re.search(r"\b(\d{1,2})[/.-](\d{1,2})(?:[/.-](\d{2,4}))?\b", t)
    if m:
        day, month = int(m.group(1)), int(m.group(2))
        if m.group(3):
            y = int(m.group(3))
            d = _safe_date(y + 2000 if y < 100 else y, month, day)
        else:
            d = _future_month_day(today, month, day)
        return _single(d) if d else None
    # "on the 12", "the 12th" -> this month, or next month if it has passed
    m = re.search(r"\b(?:on )?the (\d{1,2})\b", t)
    if m:
        day = int(m.group(1))
        d = _safe_date(today.year, today.month, day)
        if d is None or d < today:
            y, mo = (today.year + 1, 1) if today.month == 12 else (today.year, today.month + 1)
            d = _safe_date(y, mo, day)
        return _single(d) if d else None
    return None


def _clock(hour: int, minute: int, meridiem: str | None) -> time | None:
    if minute > 59 or hour > 23:
        return None
    if meridiem == "pm" and hour < 12:
        hour += 12
    elif meridiem == "am" and hour == 12:
        hour = 0
    elif meridiem is None and 1 <= hour <= 8:
        hour += 12  # "at 4" during clinic hours means 16:00
    return time(hour, minute)


_TIME = r"(\d{1,2})(?:[:.](\d{2}))? ?(am|pm|a\.m\.|p\.m\.)?"


def _time_from(m: re.Match[str], offset: int = 1) -> time | None:
    mer = (m.group(offset + 2) or "").replace(".", "") or None
    return _clock(int(m.group(offset)), int(m.group(offset + 1) or 0), mer)


def _window(start: time, end: time, label: str) -> TimeWindow:
    if end <= OPEN or start >= CLOSE:  # outside 09:00-18:00: collapse to the nearer boundary
        edge = OPEN if end <= OPEN else CLOSE
        return TimeWindow(edge, edge, label, out_of_hours=True)
    return TimeWindow(max(start, OPEN), min(end, CLOSE), label)


def resolve_time(phrase: str) -> TimeWindow | None:
    """The time-of-day window a phrase asks for (None when it names no time).

    `out_of_hours` is set when the requested time is outside 09:00-18:00.
    """
    t = _norm(phrase)
    if not t:
        return None
    if re.search(r"\b(morning|subah)\b", t):
        return TimeWindow(time(9), time(13), "morning")
    if re.search(r"\b(afternoon|after lunch|dopahar)\b", t):
        return TimeWindow(time(14), time(18), "afternoon")
    if re.search(r"\b(evening|shaam)\b", t):
        return TimeWindow(time(16), time(18), "evening")
    if re.search(r"\b(night|midnight)\b", t):
        return TimeWindow(CLOSE, CLOSE, "night", out_of_hours=True)
    if re.search(r"\b(noon|midday)\b", t):
        return _window(time(12), time(13), "around noon")
    m = re.search(r"\b(?:after|from|later than) " + _TIME, t)
    if m and (start := _time_from(m)) is not None:
        return _window(start, CLOSE, f"after {start:%H:%M}")
    m = re.search(r"\b(?:before|by|earlier than) " + _TIME, t)
    if m and (end := _time_from(m)) is not None:
        return _window(OPEN, end, f"before {end:%H:%M}")
    m = re.search(r"\b(?:around|about|near|approx(?:imately)?) " + _TIME, t)
    if m and (at := _time_from(m)) is not None:
        mins = at.hour * 60 + at.minute
        lo, hi = max(mins - 30, 0), min(mins + 30, 23 * 60 + 59)
        return _window(time(lo // 60, lo % 60), time(hi // 60, hi % 60), f"around {at:%H:%M}")
    m = re.search(r"\b(?:at )?" + _TIME + r"\b", t)
    if m and (m.group(2) or m.group(3) or t.startswith("at ") or " at " in f" {t}"):
        at = _time_from(m)
        if at is not None:
            mins = at.hour * 60 + at.minute + 15
            end = time(min(mins // 60, 23), mins % 60)
            return _window(at, end, f"at {at:%H:%M}")
    return None


def explain_unavailable(day: date, today: date, holidays: dict[date, str]) -> str | None:
    """Why the clinic cannot see anyone on `day` (None when it is a working day)."""
    if day < today:
        return f"{format_day(day)} has already passed."
    if day.weekday() == 6:
        return f"The clinic is closed on Sundays ({format_day(day)})."
    if day in holidays:
        return f"The clinic is closed on {format_day(day)} ({holidays[day]})."
    return None


def utc_bounds(
    dates: DateRange, window: TimeWindow | None, tz: ZoneInfo
) -> tuple[datetime, datetime]:
    """UTC instants covering `dates` x `window` in the clinic's zone (DST-correct: each local
    wall-clock time is converted on its own day)."""
    start_t = window.start if window else OPEN
    end_t = window.end if window else CLOSE
    start = datetime.combine(dates.start, start_t, tz).astimezone(UTC)
    end = datetime.combine(dates.end, end_t, tz).astimezone(UTC)
    return start, end
