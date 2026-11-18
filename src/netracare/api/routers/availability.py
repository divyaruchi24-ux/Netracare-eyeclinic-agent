from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta
from typing import Annotated

from fastapi import APIRouter, Query
from sqlalchemy import select

from netracare.api.deps import ClockDep, DbSession, SettingsDep
from netracare.api.schemas.availability import AvailabilityOut, DayOptions, OptionOut
from netracare.api.schemas.common import local_iso
from netracare.api.security import CurrentUserDep
from netracare.db.models import Doctor
from netracare.domain.clock import clinic_today
from netracare.domain.enums import AppointmentType
from netracare.services.availability_service import AvailabilityService

router = APIRouter(tags=["availability"])


@router.get("/availability")
def availability(
    user: CurrentUserDep,
    db: DbSession,
    clock: ClockDep,
    settings: SettingsDep,
    doctor_id: Annotated[str | None, Query(max_length=36)] = None,
    specialty: Annotated[str | None, Query(max_length=32)] = None,
    from_date: date | None = None,
    to_date: date | None = None,
    type: AppointmentType = AppointmentType.ROUTINE,
) -> AvailabilityOut:
    tz = settings.tz
    today = clinic_today(clock, tz)
    start = from_date or today
    end = to_date or (start + timedelta(days=6))
    options = AvailabilityService(db, tz).find(
        doctor_id=doctor_id,
        specialty_code=specialty,
        from_date=start,
        to_date=end,
        appt_type=type,
        now=clock.now_utc(),
    )
    names = {i: n for i, n in db.execute(select(Doctor.id, Doctor.full_name))}
    by_day: dict[date, list[OptionOut]] = defaultdict(list)
    for o in options:
        local = o.start_utc.astimezone(tz)
        by_day[local.date()].append(
            OptionOut(
                doctor_id=o.doctor_id,
                doctor_name=names.get(o.doctor_id, ""),
                start_utc=o.start_utc,
                end_utc=o.end_utc,
                start_local=local_iso(o.start_utc, tz),
                slot_ids=list(o.slot_ids),
            )
        )
    return AvailabilityOut(
        type=type,
        duration_minutes=type.duration_minutes,
        total=len(options),
        days=[DayOptions(date=d, options=opts) for d, opts in sorted(by_day.items())],
    )
