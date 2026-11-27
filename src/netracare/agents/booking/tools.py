"""Booking-agent tools: thin, typed wrappers over the service layer (the single source of truth).

Every tool
- validates its arguments with a pydantic model;
- acts as the *caller* (`CurrentUser`), so the services' access policy applies (a patient can
  only see and change their own or their linked patients' appointments);
- never raises into the graph: errors come back as `ToolResult(ok=False, error=<code>)`;
- runs writes inside a SAVEPOINT, so a failure leaves no partial writes (side effects an
  `AppError` asks to keep, such as the emergency alert raised by a booking reason, are kept);
- is idempotent: book/reschedule carry `agent:{thread}:{proposal}` keys, so a repeated "yes"
  (double click, retry after a crash) returns the original appointment instead of a second one.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, TypeVar

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from netracare.config import Settings
from netracare.db.models import Appointment, Doctor, Specialty
from netracare.domain.actor import CurrentUser
from netracare.domain.clock import Clock
from netracare.domain.enums import AppointmentStatus, AppointmentType, HandoffReason
from netracare.errors import AppError
from netracare.llm.client import LLMClient
from netracare.log import get_logger
from netracare.rag.doctor_names import DoctorName, match_doctor
from netracare.services.access import AccessPolicy
from netracare.services.availability_service import AvailabilityService
from netracare.services.booking_service import BookCommand, BookingService
from netracare.services.idempotency_service import scoped_key

log = get_logger(__name__)

ACTIVE = (
    AppointmentStatus.REQUESTED,
    AppointmentStatus.PENDING_APPROVAL,
    AppointmentStatus.CONFIRMED,
)


@dataclass
class ToolContext:
    session: Session
    clock: Clock
    settings: Settings
    actor: CurrentUser
    thread_id: str
    llm: LLMClient | None = None
    calls: list[str] = field(default_factory=list)  # tool names, in order (for evals/traces)


@dataclass(frozen=True)
class ToolResult:
    ok: bool
    data: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    message: str = ""
    details: dict[str, Any] = field(default_factory=dict)


class _Args(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SearchDoctorsArgs(_Args):
    specialty: str | None = Field(default=None, max_length=32)
    name: str | None = Field(default=None, max_length=80)


class AvailabilityArgs(_Args):
    doctor_id: str | None = Field(default=None, max_length=36)
    specialty: str | None = Field(default=None, max_length=32)
    from_date: date
    to_date: date
    appt_type: AppointmentType = AppointmentType.ROUTINE


class BookArgs(_Args):
    patient_id: str = Field(max_length=36)
    doctor_id: str = Field(max_length=36)
    slot_ids: list[str] = Field(min_length=1, max_length=4)
    appt_type: AppointmentType = AppointmentType.ROUTINE
    reason: str | None = Field(default=None, max_length=500)
    proposal_id: str = Field(max_length=64)


class CancelArgs(_Args):
    appointment_id: str = Field(max_length=36)
    reason: str | None = Field(default=None, max_length=500)


class RescheduleArgs(_Args):
    appointment_id: str = Field(max_length=36)
    slot_ids: list[str] = Field(min_length=1, max_length=4)
    reason: str | None = Field(default=None, max_length=500)
    proposal_id: str = Field(max_length=64)


class MyAppointmentsArgs(_Args):
    patient_id: str | None = Field(default=None, max_length=36)
    upcoming_only: bool = True


class EscalateArgs(_Args):
    reason: HandoffReason = HandoffReason.USER_REQUEST


A = TypeVar("A", bound=_Args)


def _run(
    ctx: ToolContext,
    name: str,
    model: type[A],
    raw: dict[str, Any],
    fn: Callable[[A], ToolResult],
    *,
    write: bool = False,
) -> ToolResult:
    ctx.calls.append(name)
    try:
        args = model.model_validate(raw)
    except ValueError as exc:
        return ToolResult(False, error="invalid_arguments", message=str(exc)[:200])
    if not write:
        try:
            return fn(args)
        except AppError as exc:
            return ToolResult(False, error=exc.code, message=exc.message, details=exc.details or {})
    savepoint = ctx.session.begin_nested()
    try:
        result = fn(args)
        ctx.session.flush()
        savepoint.commit()
        return result
    except AppError as exc:
        if exc.commit:
            savepoint.commit()  # e.g. keep the emergency alert raised by a booking reason
        else:
            savepoint.rollback()
        return ToolResult(False, error=exc.code, message=exc.message, details=exc.details or {})
    except Exception as exc:  # any other failure: roll back, report, never raise into the graph
        savepoint.rollback()
        log.warning("agent.tool_failed", tool=name, error=type(exc).__name__)
        return ToolResult(False, error="internal", message="something went wrong")


def appointment_data(a: Appointment, settings: Settings) -> dict[str, Any]:
    start = a.start_utc.astimezone(settings.tz)
    return {
        "appointment_id": a.id,
        "patient_id": a.patient_id,
        "doctor_id": a.doctor_id,
        "status": a.status.value,
        "type": a.type.value,
        "start_local": start.isoformat(),
        "end_local": a.end_utc.astimezone(settings.tz).isoformat(),
    }


def _doctor_info(s: Session, doctor_id: str) -> dict[str, Any]:
    row = s.execute(
        select(Doctor.id, Doctor.full_name, Specialty.code, Specialty.name)
        .join(Specialty, Doctor.specialty_id == Specialty.id)
        .where(Doctor.id == doctor_id)
    ).one_or_none()
    if row is None:
        return {"doctor_id": doctor_id, "doctor_name": "the doctor", "specialty": None}
    return {
        "doctor_id": row[0],
        "doctor_name": row[1],
        "specialty": row[2],
        "specialty_name": row[3],
    }


# -- read-only ---------------------------------------------------------------------------------


def search_doctors(ctx: ToolContext, **raw: Any) -> ToolResult:
    def run(a: SearchDoctorsArgs) -> ToolResult:
        stmt = (
            select(Doctor.id, Doctor.code, Doctor.full_name, Specialty.code, Specialty.name)
            .join(Specialty, Doctor.specialty_id == Specialty.id)
            .where(Doctor.is_active)
            .order_by(Doctor.code)
        )
        if a.specialty:
            stmt = stmt.where(Specialty.code == a.specialty)
        rows = ctx.session.execute(stmt).all()
        doctors = [
            {
                "doctor_id": r[0],
                "code": r[1],
                "doctor_name": r[2],
                "specialty": r[3],
                "specialty_name": r[4],
            }
            for r in rows
        ]
        if a.name:
            m = match_doctor(a.name, [DoctorName(d["code"], d["doctor_name"]) for d in doctors])
            doctors = [d for d in doctors if m and d["code"] == m.code]
        return ToolResult(True, {"doctors": doctors})

    return _run(ctx, "search_doctors", SearchDoctorsArgs, raw, run)


def get_availability(ctx: ToolContext, **raw: Any) -> ToolResult:
    def run(a: AvailabilityArgs) -> ToolResult:
        options = AvailabilityService(ctx.session, ctx.settings.tz).find(
            doctor_id=a.doctor_id,
            specialty_code=a.specialty if not a.doctor_id else None,
            from_date=a.from_date,
            to_date=a.to_date,
            appt_type=a.appt_type,
            now=ctx.clock.now_utc(),
        )
        names: dict[str, dict[str, Any]] = {}
        out = []
        for o in options:
            info = names.setdefault(o.doctor_id, _doctor_info(ctx.session, o.doctor_id))
            out.append(
                {
                    "doctor_id": o.doctor_id,
                    "doctor_name": info["doctor_name"],
                    "specialty": info.get("specialty"),
                    "start_local": o.start_utc.astimezone(ctx.settings.tz).isoformat(),
                    "end_local": o.end_utc.astimezone(ctx.settings.tz).isoformat(),
                    "slot_ids": list(o.slot_ids),
                }
            )
        return ToolResult(True, {"options": out})

    return _run(ctx, "get_availability", AvailabilityArgs, raw, run)


def get_my_appointments(ctx: ToolContext, **raw: Any) -> ToolResult:
    def run(a: MyAppointmentsArgs) -> ToolResult:
        allowed = AccessPolicy(ctx.session).patient_ids_for(ctx.actor)
        ids = [a.patient_id] if a.patient_id else sorted(allowed)
        if a.patient_id and a.patient_id not in allowed:
            return ToolResult(False, error="patient_not_found", message="patient not found")
        stmt = select(Appointment).where(Appointment.patient_id.in_(ids))
        if a.upcoming_only:
            stmt = stmt.where(
                Appointment.status.in_(ACTIVE), Appointment.start_utc > ctx.clock.now_utc()
            )
        rows = list(ctx.session.scalars(stmt.order_by(Appointment.start_utc).limit(20)))
        items = [
            {**appointment_data(r, ctx.settings), **_doctor_info(ctx.session, r.doctor_id)}
            for r in rows
        ]
        return ToolResult(True, {"appointments": items})

    return _run(ctx, "get_my_appointments", MyAppointmentsArgs, raw, run)


# -- writes ------------------------------------------------------------------------------------


def _key(ctx: ToolContext, proposal_id: str) -> str:
    return scoped_key(ctx.actor.id, f"agent:{ctx.thread_id}:{proposal_id}")


def _replay(ctx: ToolContext, key: str) -> Appointment | None:
    return ctx.session.scalar(select(Appointment).where(Appointment.idempotency_key == key))


def book_appointment(ctx: ToolContext, **raw: Any) -> ToolResult:
    def run(a: BookArgs) -> ToolResult:
        key = _key(ctx, a.proposal_id)
        prior = _replay(ctx, key)
        if prior is not None:
            data = {**appointment_data(prior, ctx.settings), "replayed": True}
            return ToolResult(True, data)
        res = BookingService(ctx.session, ctx.clock, ctx.settings, ctx.llm).book(
            ctx.actor,
            BookCommand(
                patient_id=a.patient_id,
                doctor_id=a.doctor_id,
                slot_ids=tuple(a.slot_ids),
                type=a.appt_type,
                reason=a.reason,
                idempotency_key=key,
            ),
        )
        data = {
            **appointment_data(res.appointment, ctx.settings),
            "needs_approval": res.requires_approval,
            "approval_request_id": res.approval.id if res.approval else None,
            "approval_reasons": res.approval_reasons,
        }
        return ToolResult(True, data)

    return _run(ctx, "book_appointment", BookArgs, raw, run, write=True)


def reschedule_appointment(ctx: ToolContext, **raw: Any) -> ToolResult:
    def run(a: RescheduleArgs) -> ToolResult:
        key = _key(ctx, a.proposal_id)
        prior = _replay(ctx, key)
        if prior is not None:
            return ToolResult(True, {**appointment_data(prior, ctx.settings), "replayed": True})
        res = BookingService(ctx.session, ctx.clock, ctx.settings, ctx.llm).reschedule(
            ctx.actor, a.appointment_id, a.slot_ids, a.reason, idempotency_key=key
        )
        data = {
            **appointment_data(res.appointment, ctx.settings),
            "needs_approval": res.requires_approval,
            "approval_request_id": res.approval.id if res.approval else None,
            "approval_reasons": res.approval_reasons,
        }
        return ToolResult(True, data)

    return _run(ctx, "reschedule_appointment", RescheduleArgs, raw, run, write=True)


def cancel_appointment(ctx: ToolContext, **raw: Any) -> ToolResult:
    def run(a: CancelArgs) -> ToolResult:
        appt = AccessPolicy(ctx.session).get_appointment_for_view(ctx.actor, a.appointment_id)
        if appt.status is AppointmentStatus.CANCELLED:
            return ToolResult(False, error="already_cancelled", message="already cancelled")
        res = BookingService(ctx.session, ctx.clock, ctx.settings, ctx.llm).cancel(
            ctx.actor, a.appointment_id, a.reason
        )
        data = {
            **appointment_data(res.appointment, ctx.settings),
            "cancelled": res.cancelled,
            "needs_approval": res.approval is not None,
            "approval_request_id": res.approval.id if res.approval else None,
        }
        return ToolResult(True, data)

    return _run(ctx, "cancel_appointment", CancelArgs, raw, run, write=True)


def escalate_to_human(ctx: ToolContext, chat_session: Any, **raw: Any) -> ToolResult:
    def run(a: EscalateArgs) -> ToolResult:
        from netracare.services.handoff_service import HandoffService

        h = HandoffService(ctx.session, ctx.clock).create(chat_session, a.reason, ctx.actor)
        return ToolResult(True, {"handoff_id": h.id})

    return _run(ctx, "escalate_to_human", EscalateArgs, raw, run, write=True)


def search_window(ctx: ToolContext, start: date, days: int | None = None) -> tuple[date, date]:
    """[start, start + agent_search_days) clipped to today."""
    today = ctx.clock.now_utc().astimezone(ctx.settings.tz).date()
    s = max(start, today)
    return s, s + timedelta(days=(days or ctx.settings.agent_search_days) - 1)


def as_langchain_tools(ctx: ToolContext) -> list[Any]:
    """The same tools as LangChain `StructuredTool`s (for Phase 6 MCP / Agent Skills)."""
    from langchain_core.tools import StructuredTool

    def wrap(fn: Callable[..., ToolResult], schema: type[_Args], doc: str) -> Any:
        def call(**kwargs: Any) -> dict[str, Any]:
            r = fn(ctx, **kwargs)
            return {"ok": r.ok, "data": r.data, "error": r.error, "message": r.message}

        return StructuredTool.from_function(
            func=call, name=fn.__name__, description=doc, args_schema=schema
        )

    return [
        wrap(search_doctors, SearchDoctorsArgs, "List active doctors, by specialty or name."),
        wrap(get_availability, AvailabilityArgs, "Free appointment times in a date range."),
        wrap(get_my_appointments, MyAppointmentsArgs, "The caller's upcoming appointments."),
        wrap(book_appointment, BookArgs, "Book (after an explicit yes only)."),
        wrap(cancel_appointment, CancelArgs, "Cancel (after an explicit yes only)."),
        wrap(reschedule_appointment, RescheduleArgs, "Reschedule (after an explicit yes only)."),
    ]


def now_local(ctx: ToolContext) -> datetime:
    return ctx.clock.now_utc().astimezone(ctx.settings.tz)
