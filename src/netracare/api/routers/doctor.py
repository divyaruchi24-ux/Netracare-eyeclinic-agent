from __future__ import annotations

from datetime import date, datetime, timedelta

from fastapi import APIRouter

from netracare.api.deps import ClockDep, DbSession, SettingsDep
from netracare.api.schemas.common import Schema, local_iso
from netracare.api.security import DoctorUserDep
from netracare.domain.clock import clinic_today
from netracare.domain.enums import AppointmentStatus, AppointmentType, SlotStatus
from netracare.services.doctor_service import DoctorService

router = APIRouter(tags=["doctor"])


class EntryOut(Schema):
    slot_id: str
    start_utc: datetime
    start_local: str
    slot_status: SlotStatus
    appointment_id: str | None = None
    patient_name: str | None = None
    type: AppointmentType | None = None
    status: AppointmentStatus | None = None
    reason: str | None = None


class DayOut(Schema):
    date: date
    open_slots: int
    entries: list[EntryOut]


@router.get("/doctors/me/schedule")
def my_schedule(
    user: DoctorUserDep,
    db: DbSession,
    clock: ClockDep,
    settings: SettingsDep,
    from_date: date | None = None,
    to_date: date | None = None,
) -> list[DayOut]:
    tz = settings.tz
    start = from_date or clinic_today(clock, tz)
    end = to_date or start + timedelta(days=6)
    days = DoctorService(db, tz).my_schedule(user, start, end)
    return [
        DayOut(
            date=d.day,
            open_slots=d.open_count,
            entries=[
                EntryOut(
                    slot_id=e.slot.id,
                    start_utc=e.slot.start_utc,
                    start_local=local_iso(e.slot.start_utc, tz),
                    slot_status=e.slot.status,
                    appointment_id=e.appointment.id if e.appointment else None,
                    patient_name=e.patient_name,
                    type=e.appointment.type if e.appointment else None,
                    status=e.appointment.status if e.appointment else None,
                    reason=e.appointment.reason_text if e.appointment else None,
                )
                for e in d.entries
            ],
        )
        for d in days
    ]
