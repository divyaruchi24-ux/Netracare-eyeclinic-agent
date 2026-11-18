from __future__ import annotations

from datetime import date, datetime, time
from typing import Annotated

from pydantic import AwareDatetime, Field

from netracare.api.schemas.common import Schema, ShortText


class TemplateIn(Schema):
    weekday: Annotated[int, Field(ge=0, le=6)]
    start_local: time
    end_local: time


class TemplateOut(Schema):
    id: str
    doctor_id: str
    weekday: int
    start_local: time
    end_local: time
    valid_from: date


class TemplateAdded(Schema):
    template: TemplateOut
    opened_slots: int


class LeaveIn(Schema):
    start: AwareDatetime
    end: AwareDatetime
    reason: ShortText = "leave"


class LeaveOut(Schema):
    id: str
    doctor_id: str
    start_utc: datetime
    end_utc: datetime
    reason: str


class AffectedAppointment(Schema):
    id: str
    patient_id: str
    doctor_id: str
    start_utc: datetime
    status: str


class ConflictReportOut(Schema):
    blocked_slots: int
    affected_appointments: list[AffectedAppointment]


class LeaveAdded(ConflictReportOut):
    leave: LeaveOut


class HolidayIn(Schema):
    day: date
    name: ShortText


class HolidayOut(Schema):
    day: date
    name: str


class HolidayAdded(ConflictReportOut):
    holiday: HolidayOut


class RegenerateIn(Schema):
    doctor_id: str | None = None
    weeks: Annotated[int, Field(ge=1, le=26)] = 8


class CountOut(Schema):
    count: int
