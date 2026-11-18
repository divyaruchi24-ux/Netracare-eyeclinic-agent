"""Eval tasks: functions from a case's `input` to an output dict.

`booking_policy` exercises the real approval rules (`domain.policies`). LLM-backed tasks
(red-flag classifier, router, agent) are registered here from Phase 2 onwards.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from netracare.domain.enums import AppointmentType, RedFlagLevel
from netracare.domain.policies import BookingContext, requires_approval, should_flag_for_no_shows
from netracare.evals.metrics import ExactMatch, LabelRecall, Metric, SetEquality

Task = Callable[[dict[str, Any]], dict[str, Any]]
_EVAL_NOW = datetime(2026, 11, 10, 3, 30, tzinfo=UTC)


def booking_policy_task(inp: dict[str, Any]) -> dict[str, Any]:
    level = inp.get("red_flag_level")
    ctx = BookingContext(
        appt_start_utc=_EVAL_NOW + timedelta(hours=float(inp["hours_until_start"])),
        now_utc=_EVAL_NOW,
        appointment_type=AppointmentType(inp["appointment_type"]),
        is_change=bool(inp["is_change"]),
        is_new_patient=bool(inp["is_new_patient"]),
        patient_is_minor=bool(inp["patient_is_minor"]),
        booked_by_guardian=bool(inp["booked_by_guardian"]),
        red_flag_level=RedFlagLevel(level) if level else None,
        patient_no_show_flagged=should_flag_for_no_shows(int(inp["no_show_count"])),
    )
    reasons = sorted(r.value for r in requires_approval(ctx))
    return {"reasons": reasons, "auto_confirm": not reasons}


@dataclass(frozen=True)
class EvalSuite:
    dataset: str
    task: Task | None  # None => dataset is schema-validated only (task lands in a later phase)
    metrics: list[Metric] = field(default_factory=list)
    description: str = ""


SUITES: dict[str, EvalSuite] = {
    "booking_policy": EvalSuite(
        dataset="booking_policy",
        task=booking_policy_task,
        metrics=[SetEquality("reasons"), ExactMatch("auto_confirm")],
        description="Approval rules: 24h window, surgery consult, minors, red flags, no-shows",
    ),
    "safety_redflags_v0": EvalSuite(
        dataset="safety_redflags_v0",
        task=None,
        metrics=[
            ExactMatch("level"),
            ExactMatch("route"),
            LabelRecall("level", "emergency"),
        ],
        description="Red-flag triage seed set; scored once the Phase 2 classifier exists",
    ),
}
