from __future__ import annotations

from datetime import date
from typing import Annotated

from fastapi import APIRouter, Query, status
from pydantic import model_validator

from netracare.api.deps import ClockDep, DbSession, SettingsDep
from netracare.api.schemas.appointments import AppointmentOut, BookingResponse, appointment_out
from netracare.api.schemas.common import Id, Note, Schema
from netracare.api.schemas.directory import NewPatientIn
from netracare.api.security import StaffUserDep
from netracare.domain.clock import clinic_today
from netracare.services.directory_service import NewPatient
from netracare.services.front_desk_service import FrontDeskService, WalkInCommand

router = APIRouter(prefix="/front-desk", tags=["front desk"])


class DoctorQueueOut(Schema):
    doctor_id: str
    doctor_name: str
    specialty: str
    appointments: list[AppointmentOut]


class QueueOut(Schema):
    date: date
    doctors: list[DoctorQueueOut]
    status_counts: dict[str, int]
    pending_approvals: int
    overdue_approvals: int


class WalkInIn(Schema):
    doctor_id: Id
    patient_id: Id | None = None
    new_patient: NewPatientIn | None = None
    reason: Note | None = None

    @model_validator(mode="after")
    def _one_patient(self) -> WalkInIn:
        if (self.patient_id is None) == (self.new_patient is None):
            raise ValueError("give exactly one of patient_id or new_patient")
        return self


@router.get("/queue")
def queue(
    user: StaffUserDep,
    db: DbSession,
    clock: ClockDep,
    settings: SettingsDep,
    day: Annotated[date | None, Query(alias="date")] = None,
) -> QueueOut:
    svc = FrontDeskService(db, clock, settings)
    view = svc.today_queue(day or clinic_today(clock, settings.tz))
    return QueueOut(
        date=view.day,
        doctors=[
            DoctorQueueOut(
                doctor_id=q.doctor.id,
                doctor_name=q.doctor.full_name,
                specialty=q.doctor.specialty.name,
                appointments=[appointment_out(db, a, settings.tz) for a in q.appointments],
            )
            for q in view.doctors
        ],
        status_counts=view.status_counts,
        pending_approvals=view.pending_approvals,
        overdue_approvals=view.overdue_approvals,
    )


@router.post("/walk-ins", status_code=status.HTTP_201_CREATED)
def walk_in(
    body: WalkInIn, user: StaffUserDep, db: DbSession, clock: ClockDep, settings: SettingsDep
) -> BookingResponse:
    new = NewPatient(**body.new_patient.model_dump()) if body.new_patient else None
    cmd = WalkInCommand(
        doctor_id=body.doctor_id, patient_id=body.patient_id, new_patient=new, reason=body.reason
    )
    result = FrontDeskService(db, clock, settings).walk_in(user, cmd)
    return BookingResponse(
        appointment=appointment_out(db, result.appointment, settings.tz),
        requires_approval=result.requires_approval,
        approval_reasons=result.approval_reasons,
    )
