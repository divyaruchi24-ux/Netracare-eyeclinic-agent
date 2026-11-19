"""Visit-summary drafts (doctor) and approved summaries (patient)."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter
from pydantic import Field, StringConstraints

from netracare.api.deps import ClockDep, DbSession, LLMDep, SettingsDep
from netracare.api.schemas.common import Schema, local_iso
from netracare.api.security import CurrentUserDep, DoctorUserDep
from netracare.domain.enums import SummaryStatus
from netracare.guardrails.scripts import DISCLAIMERS
from netracare.llm.schemas import VisitSummaryDraft
from netracare.services.visit_summary_service import (
    MAX_NOTES,
    MIN_NOTES,
    SummaryView,
    VisitSummaryService,
)

router = APIRouter(prefix="/appointments/{appointment_id}/visit-summary", tags=["visit summaries"])

Notes = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=MIN_NOTES, max_length=MAX_NOTES)
]


class DraftIn(Schema):
    notes: Notes


class EditIn(Schema):
    draft: VisitSummaryDraft
    acknowledged_flags: list[Annotated[str, StringConstraints(max_length=300)]] = Field(
        default_factory=list, max_length=100
    )


class SummaryOut(Schema):
    appointment_id: str
    status: SummaryStatus
    audience: str
    draft: dict[str, Any] | None = None
    final: dict[str, Any] | None = None
    notes: str | None = None
    needs_attention: list[str] = Field(default_factory=list)
    acknowledged_flags: list[str] = Field(default_factory=list)
    unresolved_flags: list[str] = Field(default_factory=list)
    doctor_name: str | None = None
    approved_at: datetime | None = None
    approved_at_local: str | None = None
    prompt_ref: str | None = None
    version: int
    disclaimer: str | None = None


def _out(v: SummaryView, settings: SettingsDep) -> SummaryOut:
    disclaimer: str | None = None
    approved_local = local_iso(v.approved_at, settings.tz) if v.approved_at else None
    if v.audience == "doctor" and v.status is not SummaryStatus.APPROVED:
        disclaimer = DISCLAIMERS["summary_draft"]
    elif v.status is SummaryStatus.APPROVED and v.approved_at:
        disclaimer = DISCLAIMERS["summary_approved"].format(
            doctor=v.doctor_name or "your doctor",
            date=v.approved_at.astimezone(settings.tz).strftime("%d %b %Y"),
        )
    return SummaryOut(
        appointment_id=v.appointment_id,
        status=v.status,
        audience=v.audience,
        draft=v.draft,
        final=v.final,
        notes=v.notes,
        needs_attention=list(v.needs_attention),
        acknowledged_flags=list(v.acknowledged_flags),
        unresolved_flags=list(v.unresolved_flags),
        doctor_name=v.doctor_name,
        approved_at=v.approved_at,
        approved_at_local=approved_local,
        prompt_ref=v.prompt_ref,
        version=v.version,
        disclaimer=disclaimer,
    )


@router.post("", response_model=SummaryOut)
def create_draft(
    appointment_id: str,
    body: DraftIn,
    user: DoctorUserDep,
    db: DbSession,
    clock: ClockDep,
    settings: SettingsDep,
    llm: LLMDep,
) -> SummaryOut:
    svc = VisitSummaryService(db, clock, settings, llm)
    return _out(svc.create_draft(user, appointment_id, body.notes), settings)


@router.get("", response_model=SummaryOut)
def get_summary(
    appointment_id: str,
    user: CurrentUserDep,
    db: DbSession,
    clock: ClockDep,
    settings: SettingsDep,
) -> SummaryOut:
    return _out(
        VisitSummaryService(db, clock, settings, None).get_for(user, appointment_id), settings
    )


@router.patch("", response_model=SummaryOut)
def edit_summary(
    appointment_id: str,
    body: EditIn,
    user: DoctorUserDep,
    db: DbSession,
    clock: ClockDep,
    settings: SettingsDep,
) -> SummaryOut:
    svc = VisitSummaryService(db, clock, settings, None)
    view = svc.edit(user, appointment_id, body.draft.model_dump(), body.acknowledged_flags)
    return _out(view, settings)


@router.post("/approve", response_model=SummaryOut)
def approve_summary(
    appointment_id: str,
    user: DoctorUserDep,
    db: DbSession,
    clock: ClockDep,
    settings: SettingsDep,
) -> SummaryOut:
    svc = VisitSummaryService(db, clock, settings, None)
    return _out(svc.approve(user, appointment_id), settings)


@router.post("/discard", response_model=SummaryOut)
def discard_summary(
    appointment_id: str,
    user: DoctorUserDep,
    db: DbSession,
    clock: ClockDep,
    settings: SettingsDep,
) -> SummaryOut:
    svc = VisitSummaryService(db, clock, settings, None)
    return _out(svc.discard(user, appointment_id), settings)
