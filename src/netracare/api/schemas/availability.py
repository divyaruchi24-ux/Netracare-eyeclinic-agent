from __future__ import annotations

from datetime import date, datetime

from netracare.api.schemas.common import Schema
from netracare.domain.enums import AppointmentType


class OptionOut(Schema):
    doctor_id: str
    doctor_name: str
    start_utc: datetime
    end_utc: datetime
    start_local: str
    slot_ids: list[str]


class DayOptions(Schema):
    date: date
    options: list[OptionOut]


class AvailabilityOut(Schema):
    type: AppointmentType
    duration_minutes: int
    total: int
    days: list[DayOptions]
