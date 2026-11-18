from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Query
from sqlalchemy.orm import Session

from netracare.api.deps import ClockDep, DbSession, SettingsDep
from netracare.api.schemas.appointments import AppointmentOut, appointment_out
from netracare.api.schemas.common import Note, Schema
from netracare.api.security import CurrentUserDep, PatientUserDep, StaffUserDep
from netracare.config import Settings
from netracare.domain.enums import ApprovalKind, ApprovalStatus
from netracare.services.approval_service import ApprovalService, ApprovalView

router = APIRouter(prefix="/approvals", tags=["approvals"])


class DecisionIn(Schema):
    note: Note | None = None


class ApprovalOut(Schema):
    id: str
    kind: ApprovalKind
    status: ApprovalStatus
    reasons: list[str]
    is_overdue: bool
    created_at: datetime
    decided_at: datetime | None
    decision_note: str | None
    request_note: str | None
    appointment: AppointmentOut
    proposed: AppointmentOut | None


def _out(db: Session, v: ApprovalView, settings: Settings) -> ApprovalOut:
    r = v.request
    return ApprovalOut(
        id=r.id,
        kind=r.kind,
        status=r.status,
        reasons=list(r.reasons),
        is_overdue=v.is_overdue,
        created_at=r.created_at,
        decided_at=r.decided_at,
        decision_note=r.decision_note,
        request_note=r.request_note,
        appointment=appointment_out(db, v.appointment, settings.tz),
        proposed=appointment_out(db, v.target, settings.tz) if v.target else None,
    )


@router.get("")
def list_approvals(
    user: StaffUserDep,
    db: DbSession,
    clock: ClockDep,
    settings: SettingsDep,
    status: Annotated[ApprovalStatus | None, Query()] = ApprovalStatus.PENDING,
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
) -> list[ApprovalOut]:
    views = ApprovalService(db, clock, settings).list(status, limit)
    return [_out(db, v, settings) for v in views]


@router.get("/{request_id}")
def get_approval(
    request_id: str, user: CurrentUserDep, db: DbSession, clock: ClockDep, settings: SettingsDep
) -> ApprovalOut:
    return _out(db, ApprovalService(db, clock, settings).get(user, request_id), settings)


@router.post("/{request_id}/approve")
def approve(
    request_id: str,
    body: DecisionIn,
    user: StaffUserDep,
    db: DbSession,
    clock: ClockDep,
    settings: SettingsDep,
) -> ApprovalOut:
    return _out(
        db, ApprovalService(db, clock, settings).approve(user, request_id, body.note), settings
    )


@router.post("/{request_id}/reject")
def reject(
    request_id: str,
    body: DecisionIn,
    user: StaffUserDep,
    db: DbSession,
    clock: ClockDep,
    settings: SettingsDep,
) -> ApprovalOut:
    return _out(
        db, ApprovalService(db, clock, settings).reject(user, request_id, body.note), settings
    )


@router.post("/{request_id}/withdraw")
def withdraw(
    request_id: str, user: PatientUserDep, db: DbSession, clock: ClockDep, settings: SettingsDep
) -> ApprovalOut:
    return _out(db, ApprovalService(db, clock, settings).withdraw(user, request_id), settings)
