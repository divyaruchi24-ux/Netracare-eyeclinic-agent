from __future__ import annotations

from datetime import datetime
from typing import Annotated
from zoneinfo import ZoneInfo

from pydantic import Field, model_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from netracare.api.schemas.common import Id, Note, Schema, local_iso
from netracare.db.models import Appointment, ApprovalRequest, Doctor, Patient
from netracare.domain.enums import AppointmentStatus, AppointmentType, ApprovalKind, ApprovalStatus
from netracare.errors import require

SlotIds = Annotated[list[Id], Field(min_length=1, max_length=2)]


class BookRequest(Schema):
    patient_id: Id
    doctor_id: Id
    slot_ids: SlotIds
    type: AppointmentType = AppointmentType.ROUTINE
    reason: Note | None = None
    override: bool = False
    override_reason: Note | None = None

    @model_validator(mode="after")
    def _override_needs_reason(self) -> BookRequest:
        if self.override and not self.override_reason:
            raise ValueError("override_reason is required when override is true")
        return self


class CancelRequest(Schema):
    reason: Note | None = None


class RescheduleRequest(Schema):
    slot_ids: SlotIds
    reason: Note | None = None


class PersonRef(Schema):
    id: str
    name: str


class DoctorRef(Schema):
    id: str
    name: str
    specialty: str


class ApprovalRef(Schema):
    id: str
    kind: ApprovalKind
    status: ApprovalStatus
    reasons: list[str]
    is_target: bool = False


class AppointmentOut(Schema):
    id: str
    patient: PersonRef
    doctor: DoctorRef
    type: AppointmentType
    status: AppointmentStatus
    start_utc: datetime
    end_utc: datetime
    start_local: str
    reason: str | None
    approval_reasons: list[str]
    approval: ApprovalRef | None
    rescheduled_from_id: str | None
    created_at: datetime


class BookingResponse(Schema):
    appointment: AppointmentOut
    requires_approval: bool
    approval_reasons: list[str]


class CancelResponse(Schema):
    appointment: AppointmentOut
    cancelled: bool
    approval: ApprovalRef | None


def _latest_approval(db: Session, appt_id: str) -> ApprovalRequest | None:
    return db.scalars(
        select(ApprovalRequest)
        .where(
            (ApprovalRequest.appointment_id == appt_id)
            | (ApprovalRequest.target_appointment_id == appt_id)
        )
        .order_by(ApprovalRequest.created_at.desc())
    ).first()


def approval_ref(req: ApprovalRequest | None, appt_id: str | None = None) -> ApprovalRef | None:
    if req is None:
        return None
    return ApprovalRef(
        id=req.id,
        kind=req.kind,
        status=req.status,
        reasons=list(req.reasons),
        is_target=appt_id is not None and req.target_appointment_id == appt_id,
    )


def appointment_out(db: Session, a: Appointment, tz: ZoneInfo) -> AppointmentOut:
    patient = require(db.get(Patient, a.patient_id), "patient_not_found")
    doctor = require(db.get(Doctor, a.doctor_id), "doctor_not_found")
    return AppointmentOut(
        id=a.id,
        patient=PersonRef(id=patient.id, name=patient.full_name),
        doctor=DoctorRef(id=doctor.id, name=doctor.full_name, specialty=doctor.specialty.name),
        type=a.type,
        status=a.status,
        start_utc=a.start_utc,
        end_utc=a.end_utc,
        start_local=local_iso(a.start_utc, tz),
        reason=a.reason_text,
        approval_reasons=list(a.approval_reasons or []),
        approval=approval_ref(_latest_approval(db, a.id), a.id),
        rescheduled_from_id=a.rescheduled_from_id,
        created_at=a.created_at,
    )
