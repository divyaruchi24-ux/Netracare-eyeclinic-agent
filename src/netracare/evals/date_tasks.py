"""Eval task for relative date/time resolution (Phase 4, offline CI gate)."""

from __future__ import annotations

from datetime import date
from typing import Any

from netracare.domain.relative_dates import resolve_dates, resolve_time


def relative_dates_task(inp: dict[str, Any]) -> dict[str, Any]:
    phrase = str(inp["phrase"])
    if inp.get("kind") == "time":
        w = resolve_time(phrase)
        if w is None:
            return {"start": None, "end": None, "out_of_hours": False}
        return {
            "start": f"{w.start:%H:%M}",
            "end": f"{w.end:%H:%M}",
            "out_of_hours": w.out_of_hours,
        }
    r = resolve_dates(phrase, date.fromisoformat(str(inp["today"])))
    if r is None:
        return {"start": None, "end": None}
    return {"start": r.start.isoformat(), "end": r.end.isoformat()}
