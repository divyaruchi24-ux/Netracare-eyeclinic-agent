from __future__ import annotations

from datetime import date

from fastapi import APIRouter, status

from netracare.api.deps import ClockDep, DbSession, SettingsDep
from netracare.api.schemas.schedule import (
    AffectedAppointment,
    ConflictReportOut,
    CountOut,
    HolidayAdded,
    HolidayIn,
    HolidayOut,
    LeaveAdded,
    LeaveIn,
    LeaveOut,
    RegenerateIn,
    TemplateAdded,
    TemplateIn,
    TemplateOut,
)
from netracare.api.security import AdminUserDep, CurrentUserDep
from netracare.db.models import Appointment
from netracare.errors import require
from netracare.services.audit_service import AuditService
from netracare.services.schedule_service import ScheduleService

router = APIRouter(tags=["schedule"])


def _affected(appts: list[Appointment]) -> list[AffectedAppointment]:
    return [
        AffectedAppointment(
            id=a.id,
            patient_id=a.patient_id,
            doctor_id=a.doctor_id,
            start_utc=a.start_utc,
            status=a.status,
        )
        for a in appts
    ]


@router.get("/doctors/{doctor_id}/schedule-templates")
def list_templates(
    doctor_id: str, user: AdminUserDep, db: DbSession, clock: ClockDep, settings: SettingsDep
) -> list[TemplateOut]:
    svc = ScheduleService(db, clock, settings)
    return [TemplateOut.model_validate(t) for t in svc.list_templates(doctor_id)]


@router.post("/doctors/{doctor_id}/schedule-templates", status_code=status.HTTP_201_CREATED)
def add_template(
    doctor_id: str,
    body: TemplateIn,
    user: AdminUserDep,
    db: DbSession,
    clock: ClockDep,
    settings: SettingsDep,
) -> TemplateAdded:
    tpl, opened = ScheduleService(db, clock, settings).add_template(
        user, doctor_id, body.weekday, body.start_local, body.end_local
    )
    return TemplateAdded(template=TemplateOut.model_validate(tpl), opened_slots=opened)


@router.delete("/schedule-templates/{template_id}")
def remove_template(
    template_id: str, user: AdminUserDep, db: DbSession, clock: ClockDep, settings: SettingsDep
) -> ConflictReportOut:
    rep = ScheduleService(db, clock, settings).remove_template(user, template_id)
    return ConflictReportOut(
        blocked_slots=rep.blocked_slots, affected_appointments=_affected(rep.affected_appointments)
    )


@router.get("/doctors/{doctor_id}/leaves")
def list_leaves(
    doctor_id: str, user: AdminUserDep, db: DbSession, clock: ClockDep, settings: SettingsDep
) -> list[LeaveOut]:
    return [
        LeaveOut.model_validate(lv)
        for lv in ScheduleService(db, clock, settings).list_leaves(doctor_id)
    ]


@router.post("/doctors/{doctor_id}/leaves", status_code=status.HTTP_201_CREATED)
def add_leave(
    doctor_id: str,
    body: LeaveIn,
    user: AdminUserDep,
    db: DbSession,
    clock: ClockDep,
    settings: SettingsDep,
) -> LeaveAdded:
    res = ScheduleService(db, clock, settings).add_leave(
        user, doctor_id, body.start, body.end, body.reason
    )
    return LeaveAdded(
        leave=LeaveOut.model_validate(require(res.leave)),
        blocked_slots=res.blocked_slots,
        affected_appointments=_affected(res.affected_appointments),
    )


@router.delete("/leaves/{leave_id}")
def remove_leave(
    leave_id: str, user: AdminUserDep, db: DbSession, clock: ClockDep, settings: SettingsDep
) -> CountOut:
    return CountOut(count=ScheduleService(db, clock, settings).remove_leave(user, leave_id))


@router.get("/holidays")
def list_holidays(
    user: CurrentUserDep, db: DbSession, clock: ClockDep, settings: SettingsDep
) -> list[HolidayOut]:
    return [
        HolidayOut.model_validate(h) for h in ScheduleService(db, clock, settings).list_holidays()
    ]


@router.post("/holidays", status_code=status.HTTP_201_CREATED)
def add_holiday(
    body: HolidayIn, user: AdminUserDep, db: DbSession, clock: ClockDep, settings: SettingsDep
) -> HolidayAdded:
    res = ScheduleService(db, clock, settings).add_holiday(user, body.day, body.name)
    return HolidayAdded(
        holiday=HolidayOut.model_validate(require(res.holiday)),
        blocked_slots=res.blocked_slots,
        affected_appointments=_affected(res.affected_appointments),
    )


@router.delete("/holidays/{day}")
def remove_holiday(
    day: date, user: AdminUserDep, db: DbSession, clock: ClockDep, settings: SettingsDep
) -> CountOut:
    return CountOut(count=ScheduleService(db, clock, settings).remove_holiday(user, day))


@router.post("/admin/slots/regenerate")
def regenerate(
    body: RegenerateIn, user: AdminUserDep, db: DbSession, clock: ClockDep, settings: SettingsDep
) -> CountOut:
    n = ScheduleService(db, clock, settings).regenerate_slots(body.doctor_id, body.weeks)
    AuditService(db).record(
        user,
        "schedule.regenerate",
        "doctor",
        body.doctor_id,
        after={"inserted": n, "weeks": body.weeks},
    )
    return CountOut(count=n)
