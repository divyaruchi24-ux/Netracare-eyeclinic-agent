"""Admin scheduling: weekly templates, leaves, holidays and slot regeneration.

Slot status is always *derived* from the same rules as `generate_slots` (templates, lunch,
leaves, holidays). Removing a leave/holiday re-opens only slots those rules allow; booked
slots are never touched - affected appointments are reported and patients notified instead.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from netracare.config import Settings
from netracare.db.models import Appointment, Doctor, Holiday, Leave, ScheduleTemplate
from netracare.domain.actor import CurrentUser
from netracare.domain.clock import Clock, clinic_today, ensure_utc
from netracare.domain.enums import ACTIVE_STATUSES, SlotStatus
from netracare.domain.slot_generation import WorkBlock, generate_slots
from netracare.errors import ConflictError, NotFoundError, ValidationAppError
from netracare.repositories.slot_repo import SlotRepository
from netracare.services.audit_service import AuditService
from netracare.services.notification_service import NotificationService

CLINIC_OPEN = time(9, 0)
CLINIC_CLOSE = time(18, 0)
MAX_LEAVE = timedelta(days=90)
DEFAULT_WEEKS = 8


@dataclass
class ConflictReport:
    blocked_slots: int = 0
    affected_appointments: list[Appointment] = field(default_factory=list)


@dataclass
class LeaveResult(ConflictReport):
    leave: Leave | None = None


@dataclass
class HolidayResult(ConflictReport):
    holiday: Holiday | None = None


class ScheduleService:
    def __init__(self, session: Session, clock: Clock, settings: Settings) -> None:
        self.s = session
        self.clock = clock
        self.tz = settings.tz
        self.slots = SlotRepository(session)
        self.audit = AuditService(session)
        self.notify = NotificationService(session, settings.tz)

    # -- helpers -----------------------------------------------------------------------------
    def _doctor(self, doctor_id: str) -> Doctor:
        doctor = self.s.get(Doctor, doctor_id)
        if doctor is None:
            raise NotFoundError("doctor_not_found", "doctor not found")
        return doctor

    def _today(self) -> date:
        return clinic_today(self.clock, self.tz)

    def _blocks(self, doctor_id: str) -> list[WorkBlock]:
        return [
            WorkBlock(t.weekday, t.start_local, t.end_local)
            for t in self.s.scalars(
                select(ScheduleTemplate).where(ScheduleTemplate.doctor_id == doctor_id)
            )
        ]

    def _leaves(self, doctor_id: str) -> list[tuple[datetime, datetime]]:
        return [
            (lv.start_utc, lv.end_utc)
            for lv in self.s.scalars(select(Leave).where(Leave.doctor_id == doctor_id))
        ]

    def _holidays(self) -> set[date]:
        return set(self.s.scalars(select(Holiday.day)))

    def _eligible_starts(self, doctor_id: str, first: date, days: int) -> set[datetime]:
        specs = generate_slots(
            doctor_id,
            self._blocks(doctor_id),
            self._leaves(doctor_id),
            self._holidays(),
            first,
            days,
            self.tz,
        )
        return {s.start_utc for s in specs}

    def _local_range(self, first: date, last: date) -> tuple[datetime, datetime]:
        start = datetime.combine(first, time(0), self.tz)
        end = datetime.combine(last + timedelta(days=1), time(0), self.tz)
        return ensure_utc(start), ensure_utc(end)

    def _affected(
        self, start: datetime, end: datetime, doctor_id: str | None = None
    ) -> list[Appointment]:
        stmt = select(Appointment).where(
            Appointment.status.in_(ACTIVE_STATUSES),
            Appointment.start_utc < end,
            Appointment.end_utc > start,
        )
        if doctor_id is not None:
            stmt = stmt.where(Appointment.doctor_id == doctor_id)
        return list(self.s.scalars(stmt.order_by(Appointment.start_utc)))

    def _resync(self, doctor_id: str, first: date, last: date) -> tuple[int, int]:
        """Re-derive OPEN/BLOCKED for slots in [first, last]; returns (opened, blocked)."""
        first = max(first, self._today())
        if last < first:
            return 0, 0
        eligible = self._eligible_starts(doctor_id, first, (last - first).days + 1)
        start, end = self._local_range(first, last)
        opened = blocked = 0
        for slot in self.slots.for_doctor_between(doctor_id, start, end):
            should_open = slot.start_utc in eligible
            if should_open and slot.status is SlotStatus.BLOCKED:
                slot.status = SlotStatus.OPEN
                opened += 1
            elif not should_open and slot.status is SlotStatus.OPEN:
                slot.status = SlotStatus.BLOCKED
                blocked += 1
        self.s.flush()
        return opened, blocked

    # -- slots -------------------------------------------------------------------------------
    def regenerate_slots(self, doctor_id: str | None = None, weeks: int = DEFAULT_WEEKS) -> int:
        """Materialise any missing slots for the rolling window. Idempotent."""
        if not 1 <= weeks <= 26:
            raise ValidationAppError("invalid_weeks", "weeks must be between 1 and 26")
        ids = (
            [self._doctor(doctor_id).id]
            if doctor_id
            else list(self.s.scalars(select(Doctor.id).where(Doctor.is_active)))
        )
        today = self._today()
        holidays = self._holidays()
        inserted = 0
        for did in ids:
            specs = generate_slots(
                did, self._blocks(did), self._leaves(did), holidays, today, weeks * 7, self.tz
            )
            inserted += self.slots.upsert_many(specs)
        return inserted

    # -- templates ---------------------------------------------------------------------------
    def list_templates(self, doctor_id: str) -> list[ScheduleTemplate]:
        self._doctor(doctor_id)
        return list(
            self.s.scalars(
                select(ScheduleTemplate)
                .where(ScheduleTemplate.doctor_id == doctor_id)
                .order_by(ScheduleTemplate.weekday, ScheduleTemplate.start_local)
            )
        )

    def add_template(
        self, actor: CurrentUser, doctor_id: str, weekday: int, start: time, end: time
    ) -> tuple[ScheduleTemplate, int]:
        self._doctor(doctor_id)
        if not 0 <= weekday <= 5:
            raise ValidationAppError("invalid_weekday", "weekday must be 0 (Mon) .. 5 (Sat)")
        if not (CLINIC_OPEN <= start < end <= CLINIC_CLOSE):
            raise ValidationAppError("outside_clinic_hours", "blocks must fall within 09:00-18:00")
        if start.minute % 15 or end.minute % 15 or start.second or end.second:
            raise ValidationAppError("not_on_slot_grid", "times must be on a 15-minute grid")
        for t in self.list_templates(doctor_id):
            if t.weekday == weekday and t.start_local < end and start < t.end_local:
                raise ConflictError(
                    "template_overlap", "overlaps an existing block", details={"id": t.id}
                )
        tpl = ScheduleTemplate(
            doctor_id=doctor_id,
            weekday=weekday,
            start_local=start,
            end_local=end,
            valid_from=self._today(),
        )
        self.s.add(tpl)
        self.s.flush()
        inserted = self.regenerate_slots(doctor_id)
        today = self._today()
        reopened, _ = self._resync(doctor_id, today, today + timedelta(weeks=DEFAULT_WEEKS))
        opened = inserted + reopened
        self.audit.record(
            actor,
            "schedule.template_add",
            "doctor",
            doctor_id,
            after={"weekday": weekday, "start": start.isoformat(), "end": end.isoformat()},
        )
        return tpl, opened

    def remove_template(self, actor: CurrentUser, template_id: str) -> ConflictReport:
        tpl = self.s.get(ScheduleTemplate, template_id)
        if tpl is None:
            raise NotFoundError("template_not_found", "schedule block not found")
        doctor_id = tpl.doctor_id
        snapshot = {
            "weekday": tpl.weekday,
            "start": tpl.start_local.isoformat(),
            "end": tpl.end_local.isoformat(),
        }
        self.s.delete(tpl)
        self.s.flush()
        today = self._today()
        horizon = today + timedelta(weeks=DEFAULT_WEEKS)
        _, blocked = self._resync(doctor_id, today, horizon)
        # Report bookings that now sit outside the doctor's working hours.
        eligible = self._eligible_starts(doctor_id, today, (horizon - today).days + 1)
        start, end = self._local_range(today, horizon)
        affected = [a for a in self._affected(start, end, doctor_id) if a.start_utc not in eligible]
        self.audit.record(
            actor,
            "schedule.template_remove",
            "doctor",
            doctor_id,
            before=snapshot,
            after={"blocked_slots": blocked, "affected": [a.id for a in affected]},
        )
        return ConflictReport(blocked, affected)

    # -- leaves ------------------------------------------------------------------------------
    def list_leaves(self, doctor_id: str) -> list[Leave]:
        self._doctor(doctor_id)
        return list(
            self.s.scalars(
                select(Leave).where(Leave.doctor_id == doctor_id).order_by(Leave.start_utc)
            )
        )

    def add_leave(
        self, actor: CurrentUser, doctor_id: str, start: datetime, end: datetime, reason: str
    ) -> LeaveResult:
        doctor = self._doctor(doctor_id)
        start, end = ensure_utc(start), ensure_utc(end)
        if start >= end:
            raise ValidationAppError("invalid_range", "leave end must be after its start")
        if end - start > MAX_LEAVE:
            raise ValidationAppError("leave_too_long", "a single leave can be at most 90 days")
        if end <= self.clock.now_utc():
            raise ValidationAppError("leave_in_past", "leave must end in the future")
        for lv in self.list_leaves(doctor_id):
            if lv.start_utc < end and start < lv.end_utc:
                raise ConflictError(
                    "leave_overlap", "overlaps an existing leave", details={"id": lv.id}
                )
        leave = Leave(doctor_id=doctor.id, start_utc=start, end_utc=end, reason=reason)
        self.s.add(leave)
        self.s.flush()
        blocked = self.slots.block_range(doctor.id, start, end)
        affected = self._affected(start, end, doctor.id)
        for appt in affected:
            self.notify.for_appointment(appt, "leave_conflict")
        self.audit.record(
            actor,
            "schedule.leave_add",
            "leave",
            leave.id,
            after={
                "doctor_id": doctor.id,
                "start_utc": start.isoformat(),
                "end_utc": end.isoformat(),
                "blocked_slots": blocked,
                "affected": [a.id for a in affected],
            },
            reason=reason,
        )
        return LeaveResult(blocked, affected, leave)

    def remove_leave(self, actor: CurrentUser, leave_id: str) -> int:
        leave = self.s.get(Leave, leave_id)
        if leave is None:
            raise NotFoundError("leave_not_found", "leave not found")
        doctor_id, start, end = leave.doctor_id, leave.start_utc, leave.end_utc
        self.s.delete(leave)
        self.s.flush()
        self.regenerate_slots(doctor_id)
        first = start.astimezone(self.tz).date()
        last = end.astimezone(self.tz).date()
        opened, _ = self._resync(doctor_id, first, last)
        self.audit.record(
            actor,
            "schedule.leave_remove",
            "leave",
            leave_id,
            before={
                "doctor_id": doctor_id,
                "start_utc": start.isoformat(),
                "end_utc": end.isoformat(),
            },
            after={"reopened_slots": opened},
        )
        return opened

    # -- holidays ----------------------------------------------------------------------------
    def list_holidays(self) -> list[Holiday]:
        return list(self.s.scalars(select(Holiday).order_by(Holiday.day)))

    def add_holiday(self, actor: CurrentUser, day: date, name: str) -> HolidayResult:
        if day < self._today():
            raise ValidationAppError("holiday_in_past", "holiday must be today or later")
        if self.s.get(Holiday, day) is not None:
            raise ConflictError("holiday_exists", "a holiday already exists on that day")
        holiday = Holiday(day=day, name=name)
        self.s.add(holiday)
        self.s.flush()
        start, end = self._local_range(day, day)
        blocked = 0
        for did in self.s.scalars(select(Doctor.id)):
            blocked += self.slots.block_range(did, start, end)
        affected = self._affected(start, end)
        for appt in affected:
            self.notify.for_appointment(appt, "holiday_conflict")
        self.audit.record(
            actor,
            "schedule.holiday_add",
            "holiday",
            None,
            after={
                "day": day.isoformat(),
                "name": name,
                "blocked_slots": blocked,
                "affected": [a.id for a in affected],
            },
        )
        return HolidayResult(blocked, affected, holiday)

    def remove_holiday(self, actor: CurrentUser, day: date) -> int:
        holiday = self.s.get(Holiday, day)
        if holiday is None:
            raise NotFoundError("holiday_not_found", "holiday not found")
        self.s.delete(holiday)
        self.s.flush()
        self.regenerate_slots()
        opened = 0
        for did in self.s.scalars(select(Doctor.id).where(Doctor.is_active)):
            opened += self._resync(did, day, day)[0]
        self.audit.record(
            actor,
            "schedule.holiday_remove",
            "holiday",
            None,
            before={"day": day.isoformat(), "name": holiday.name},
            after={"reopened_slots": opened},
        )
        return opened
