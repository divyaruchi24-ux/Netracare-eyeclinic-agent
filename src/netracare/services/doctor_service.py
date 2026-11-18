"""A doctor's own schedule (slots with booking summaries)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from netracare.db.models import Appointment, AppointmentSlot, Patient, Slot
from netracare.domain.actor import CurrentUser
from netracare.domain.clock import ensure_utc
from netracare.domain.enums import SlotStatus
from netracare.errors import ForbiddenError, ValidationAppError

MAX_DAYS = 14


@dataclass(frozen=True)
class ScheduleEntry:
    slot: Slot
    appointment: Appointment | None
    patient_name: str | None


@dataclass
class ScheduleDay:
    day: date
    entries: list[ScheduleEntry] = field(default_factory=list)

    @property
    def open_count(self) -> int:
        return sum(e.slot.status is SlotStatus.OPEN and e.appointment is None for e in self.entries)


class DoctorService:
    def __init__(self, session: Session, tz: ZoneInfo) -> None:
        self.s = session
        self.tz = tz

    def my_schedule(self, user: CurrentUser, from_date: date, to_date: date) -> list[ScheduleDay]:
        if user.doctor_id is None:
            raise ForbiddenError("forbidden", "only doctors have a schedule")
        if to_date < from_date or (to_date - from_date).days + 1 > MAX_DAYS:
            raise ValidationAppError("invalid_range", f"choose a range of 1-{MAX_DAYS} days")
        start = ensure_utc(datetime.combine(from_date, time(0), self.tz))
        end = ensure_utc(datetime.combine(to_date + timedelta(days=1), time(0), self.tz))
        rows = self.s.execute(
            select(Slot, Appointment, Patient.full_name)
            .outerjoin(
                AppointmentSlot, (AppointmentSlot.slot_id == Slot.id) & AppointmentSlot.active
            )
            .outerjoin(Appointment, Appointment.id == AppointmentSlot.appointment_id)
            .outerjoin(Patient, Patient.id == Appointment.patient_id)
            .where(Slot.doctor_id == user.doctor_id, Slot.start_utc >= start, Slot.start_utc < end)
            .order_by(Slot.start_utc)
        )
        days: dict[date, ScheduleDay] = {}
        for slot, appt, name in rows:
            d = slot.start_utc.astimezone(self.tz).date()
            days.setdefault(d, ScheduleDay(d)).entries.append(ScheduleEntry(slot, appt, name))
        return [days[d] for d in sorted(days)]
