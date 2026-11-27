"""Booking-agent state (checkpointed) and runtime (per request, never checkpointed).

State holds only ids, ISO dates, labels and the flow step: JSON-friendly and PII-light.
The runtime carries the request's DB session, the caller's identity and the LLM client.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any, TypedDict

from sqlalchemy.orm import Session

from netracare.config import Settings
from netracare.domain.actor import CurrentUser
from netracare.domain.clock import Clock
from netracare.llm.client import LLMClient


class BookingState(TypedDict, total=False):
    # this turn
    input: str
    event: dict[str, Any] | None
    nlu: dict[str, Any] | None
    reply: dict[str, Any]
    turn: int
    expired_note: bool  # per-turn flags (reset by `ingest`)
    resumed: bool
    out_of_hours: bool
    # flow
    action: str  # BookingAction value
    step: str  # AgentStep value
    step_before_pause: str | None
    patient_id: str | None
    patient_name: str | None
    patient_is_minor: bool
    doctor_id: str | None
    doctor_name: str | None
    specialty: str | None
    concern: str | None
    date_from: str | None
    date_to: str | None
    date_label: str | None
    asked_dates: bool
    time_from: str | None
    time_to: str | None
    time_label: str | None
    appt_type: str
    appointment: dict[str, Any] | None  # target of reschedule / cancel
    appointment_choices: list[dict[str, Any]]
    options: list[dict[str, Any]]
    proposal_id: str | None
    proposed_at: str | None
    chosen: dict[str, Any] | None
    approval_reasons: list[str]
    readback_turn: int
    failures: int
    # across flows
    pending: list[dict[str, Any]]
    writes: list[dict[str, Any]]
    updated_at: str | None
    flows: int


FLOW_KEYS = (
    "action",
    "step",
    "step_before_pause",
    "patient_id",
    "patient_name",
    "patient_is_minor",
    "doctor_id",
    "doctor_name",
    "specialty",
    "concern",
    "date_from",
    "date_to",
    "date_label",
    "asked_dates",
    "time_from",
    "time_to",
    "time_label",
    "appt_type",
    "appointment",
    "appointment_choices",
    "options",
    "proposal_id",
    "proposed_at",
    "chosen",
    "approval_reasons",
    "readback_turn",
    "failures",
)


def fresh_flow() -> dict[str, Any]:
    """Values that reset when a new booking flow starts (pending/writes persist)."""
    return {
        "action": "none",
        "step": "collect",
        "step_before_pause": None,
        "patient_id": None,
        "patient_name": None,
        "patient_is_minor": False,
        "doctor_id": None,
        "doctor_name": None,
        "specialty": None,
        "concern": None,
        "date_from": None,
        "date_to": None,
        "date_label": None,
        "asked_dates": False,
        "time_from": None,
        "time_to": None,
        "time_label": None,
        "appt_type": "routine",
        "appointment": None,
        "appointment_choices": [],
        "options": [],
        "proposal_id": None,
        "proposed_at": None,
        "chosen": None,
        "approval_reasons": [],
        "readback_turn": -1,
        "failures": 0,
    }


@dataclass
class PatientChoice:
    id: str
    name: str
    is_minor: bool
    is_self: bool


@dataclass
class AgentRuntime:
    session: Session
    clock: Clock
    settings: Settings
    actor: CurrentUser
    thread_id: str
    llm: LLMClient | None = None
    chat_session: Any = None  # the ChatSession row (for escalate_to_human)
    patients: list[PatientChoice] = field(default_factory=list)
    holidays: dict[date, str] = field(default_factory=dict)
    redact_names: list[str] = field(default_factory=list)
    llm_call_ids: list[str] = field(default_factory=list)
    cost_usd: float = 0.0
    tool_calls: list[str] = field(default_factory=list)
