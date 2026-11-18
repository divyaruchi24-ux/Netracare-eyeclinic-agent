"""Front-desk day operations: today's queue and walk-ins."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from netracare.config import Settings
from netracare.db.models import Appointment, AppointmentSlot, Doctor, Slot
from netracare.domain.actor import CurrentUser
from netracare.domain.clock import Clock, ensure_utc
from netracare.domain.enums import AppointmentStatus, AppointmentType, ApprovalStatus, SlotStatus
from netracare.errors import ConflictError, ValidationAppError, require
from netracare.services.approval_service import ApprovalService
from netracare.services.booking_service import BookCommand, BookingResult, BookingService
from netracare.services.directory_service import DirectoryService, NewPatient

_QUEUE_STATUSES = frozenset(
    {
        AppointmentStatus.PENDING_APPROVAL,
        AppointmentStatus.CONFIRMED,
        AppointmentStatus.CHECKED_IN,
        AppointmentStatus.COMPLETED,
        AppointmentStatus.NO_SHOW,
    }
)


@dataclass
class DoctorQueue:
    doctor: Doctor
    appointments: list[Appointment] = field(default_factory=list)


@dataclass
class QueueView:
    day: date
    doctors: list[DoctorQueue]
    status_counts: dict[str, int]
    pending_approvals: int
    overdue_approvals: int


@dataclass(frozen=True)
class WalkInCommand:
    doctor_id: str
    patient_id: str | None = None
    new_patient: NewPatient | None = None
    reason: str | None = None


class FrontDeskService:
    def __init__(self, session: Session, clock: Clock, settings: Settings) -> None:
        self.s = session
        self.clock = clock
        self.settings = settings
        self.tz = settings.tz

    def _day_range(self, day: date) -> tuple[datetime, datetime]:
        start = ensure_utc(datetime.combine(day, time(0), self.tz))
        return start, start + timedelta(days=1)

    def today_queue(self, day: date) -> QueueView:
        start, end = self._day_range(day)
        appts = list(
            self.s.scalars(
                select(Appointment)
                .where(
                    Appointment.start_utc >= start,
                    Appointment.start_utc < end,
                    Appointment.status.in_(_QUEUE_STATUSES),
                )
                .order_by(Appointment.start_utc)
            )
        )
        by_doctor: dict[str, DoctorQueue] = {}
        for d in self.s.scalars(select(Doctor).where(Doctor.is_active).order_by(Doctor.code)):
            by_doctor[d.id] = DoctorQueue(d)
        for a in appts:
            if a.doctor_id not in by_doctor:
                doc = require(self.s.get(Doctor, a.doctor_id), "doctor_not_found")
                by_doctor[a.doctor_id] = DoctorQueue(doc)
            by_doctor[a.doctor_id].appointments.append(a)
        pending = ApprovalService(self.s, self.clock, self.settings).list(ApprovalStatus.PENDING)
        return QueueView(
            day=day,
            doctors=list(by_doctor.values()),
            status_counts=dict(Counter(a.status.value for a in appts)),
            pending_approvals=len(pending),
            overdue_approvals=sum(v.is_overdue for v in pending),
        )

    def next_open_slot_today(self, doctor_id: str) -> Slot:
        now = self.clock.now_utc()
        _, end = self._day_range(now.astimezone(self.tz).date())
        held = select(AppointmentSlot.slot_id).where(AppointmentSlot.active)
        slot = self.s.scalars(
            select(Slot)
            .where(
                Slot.doctor_id == doctor_id,
                Slot.status == SlotStatus.OPEN,
                Slot.start_utc > now,
                Slot.start_utc < end,
                Slot.id.not_in(held),
            )
            .order_by(Slot.start_utc)
        ).first()
        if slot is None:
            raise ConflictError("no_slots_today", "this doctor has no free slot left today")
        return slot

    def walk_in(self, actor: CurrentUser, cmd: WalkInCommand) -> BookingResult:
        if (cmd.patient_id is None) == (cmd.new_patient is None):
            raise ValidationAppError(
                "patient_or_new_patient",
                "give either an existing patient_id or new patient details",
            )
        booking = BookingService(self.s, self.clock, self.settings)
        booking._doctor(cmd.doctor_id)  # validates existence/active before creating a patient
        slot = self.next_open_slot_today(cmd.doctor_id)
        if cmd.new_patient is not None:
            patient_id = (
                DirectoryService(self.s, self.clock).create_patient(actor, cmd.new_patient).id
            )
        else:
            patient_id = require(cmd.patient_id, "patient_not_found")
        return booking.book(
            actor,
            BookCommand(
                patient_id=patient_id,
                doctor_id=cmd.doctor_id,
                slot_ids=(slot.id,),
                type=AppointmentType.ROUTINE,
                reason=cmd.reason,
                override=True,
                override_reason="walk-in at front desk",
            ),
        )
