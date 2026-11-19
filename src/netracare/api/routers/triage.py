"""Symptom triage (public) and front-desk red-flag alerts (staff)."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Query, Request
from pydantic import StringConstraints
from sqlalchemy import select

from netracare.api.deps import ClockDep, DbSession, LLMDep, SettingsDep
from netracare.api.schemas.common import Id, Note, Schema
from netracare.api.security import OptionalUserDep, StaffUserDep
from netracare.db.models import Patient, TriageAlert
from netracare.domain.enums import AlertStatus, RedFlagLevel, Role
from netracare.guardrails.scripts import DISCLAIMERS
from netracare.security.rate_limit import SlidingWindowLimiter
from netracare.services.access import AccessPolicy
from netracare.services.triage_service import MAX_TEXT, TriageResult, TriageService

router = APIRouter(prefix="/triage", tags=["triage"])

TriageText = Annotated[str, StringConstraints(min_length=1, max_length=MAX_TEXT)]


class TriageIn(Schema):
    text: TriageText
    patient_id: Id | None = None


class TriageOut(Schema):
    level: RedFlagLevel
    codes: list[str]
    specialty_code: str | None
    doctor_name: str | None
    confidence: float | None
    source: str
    message: str
    alert_id: str | None
    needs_handoff: bool
    disclaimer: str


class AlertOut(Schema):
    id: str
    created_at: datetime
    level: RedFlagLevel
    codes: list[str]
    confidence: float | None
    source: str
    excerpt_redacted: str
    patient_id: str | None
    patient_name: str | None = None
    status: AlertStatus
    acknowledged_by: str | None
    acknowledged_at: datetime | None
    resolved_by: str | None
    resolved_at: datetime | None
    resolution_note: str | None
    version: int


class AlertList(Schema):
    items: list[AlertOut]
    open_emergencies: int
    open_total: int


class ResolveIn(Schema):
    note: Note


def triage_out(r: TriageResult) -> TriageOut:
    return TriageOut(
        level=r.level,
        codes=r.codes,
        specialty_code=r.specialty_code,
        doctor_name=r.doctor_name,
        confidence=r.confidence,
        source=r.source,
        message=r.message,
        alert_id=r.alert_id,
        needs_handoff=r.needs_handoff,
        disclaimer=DISCLAIMERS["chat_footer"],
    )


def _alert_out(db: DbSession, a: TriageAlert) -> AlertOut:
    out = AlertOut.model_validate(a)
    if a.patient_id:
        out.patient_name = db.get(Patient, a.patient_id).full_name  # type: ignore[union-attr]
    return out


@router.post("", response_model=TriageOut)
def triage(
    body: TriageIn,
    request: Request,
    user: OptionalUserDep,
    db: DbSession,
    clock: ClockDep,
    settings: SettingsDep,
    llm: LLMDep,
) -> TriageOut:
    limiter: SlidingWindowLimiter = request.app.state.triage_limiter
    client_ip = request.client.host if request.client else "unknown"
    limiter.hit(f"user:{user.id}" if user else f"ip:{client_ip}", clock.now_utc())

    patient_id = body.patient_id
    if user is not None and patient_id is not None:
        AccessPolicy(db).get_patient_for_view(user, patient_id)  # 404 if not theirs
    elif user is not None and user.role is Role.PATIENT:
        patient_id = db.scalar(select(Patient.id).where(Patient.user_id == user.id))
    elif user is None:
        patient_id = None  # anonymous callers cannot attach an alert to a patient record
    result = TriageService(db, clock, settings, llm).classify(
        body.text, actor=user, patient_id=patient_id
    )
    return triage_out(result)


@router.get("/alerts", response_model=AlertList)
def list_alerts(
    user: StaffUserDep,
    db: DbSession,
    clock: ClockDep,
    settings: SettingsDep,
    status: AlertStatus | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
) -> AlertList:
    svc = TriageService(db, clock, settings, None)
    return AlertList(
        items=[_alert_out(db, a) for a in svc.list_alerts(status, limit)],
        open_emergencies=svc.count_open(RedFlagLevel.EMERGENCY),
        open_total=svc.count_open(),
    )


@router.post("/alerts/{alert_id}/ack", response_model=AlertOut)
def ack_alert(
    alert_id: str,
    user: StaffUserDep,
    db: DbSession,
    clock: ClockDep,
    settings: SettingsDep,
    expected_version: int | None = None,
) -> AlertOut:
    alert = TriageService(db, clock, settings, None).ack(
        user, alert_id, expected_version=expected_version
    )
    return _alert_out(db, alert)


@router.post("/alerts/{alert_id}/resolve", response_model=AlertOut)
def resolve_alert(
    alert_id: str,
    body: ResolveIn,
    user: StaffUserDep,
    db: DbSession,
    clock: ClockDep,
    settings: SettingsDep,
) -> AlertOut:
    alert = TriageService(db, clock, settings, None).resolve(user, alert_id, body.note)
    return _alert_out(db, alert)
