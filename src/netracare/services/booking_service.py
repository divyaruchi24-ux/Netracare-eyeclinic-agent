"""Booking, cancellation, rescheduling and visit-day transitions.

Safety nets, outermost first:
1. ownership (AccessPolicy) and role checks;
2. slot validation (exists, same doctor, open, future, consecutive, unheld);
3. duplicate/overlap checks for the patient;
4. the partial unique index `uq_active_slot_hold` - the final guard if two writers race
   (IntegrityError -> 409 `slot_unavailable`);
5. everything (appointment, holds, approval request, audit, outbox) in one transaction.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from itertools import pairwise

from sqlalchemy import exists, select, true
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from netracare.config import Settings
from netracare.db.models import (
    Appointment,
    AppointmentSlot,
    ApprovalRequest,
    Doctor,
    Patient,
    Slot,
)
from netracare.domain.actor import CurrentUser
from netracare.domain.clock import Clock, clinic_today
from netracare.domain.enums import (
    ACTIVE_STATUSES,
    AppointmentEvent,
    AppointmentStatus,
    AppointmentType,
    ApprovalKind,
    ApprovalReason,
    ApprovalStatus,
    Role,
    SlotStatus,
)
from netracare.domain.policies import (
    BookingContext,
    is_minor,
    is_within_change_window,
    requires_approval,
    should_flag_for_no_shows,
)
from netracare.domain.state_machine import Actor, transition
from netracare.errors import (
    ConflictError,
    ForbiddenError,
    NotFoundError,
    ValidationAppError,
    require,
)
from netracare.observability.tracing import traced
from netracare.repositories.appointment_repo import AppointmentRepository
from netracare.services.access import AccessPolicy
from netracare.services.audit_service import AuditService
from netracare.services.availability_service import slots_needed
from netracare.services.notification_service import NotificationService

CHECK_IN_GRACE = timedelta(hours=1)
_SYSTEM: Actor = "system"


@dataclass(frozen=True)
class BookCommand:
    patient_id: str
    doctor_id: str
    slot_ids: tuple[str, ...]
    type: AppointmentType
    reason: str | None = None
    idempotency_key: str | None = None  # already user-scoped (see idempotency.scoped_key)
    override: bool = False
    override_reason: str | None = None


@dataclass
class BookingResult:
    appointment: Appointment
    approval: ApprovalRequest | None = None
    approval_reasons: list[str] = field(default_factory=list)

    @property
    def requires_approval(self) -> bool:
        return self.approval is not None


@dataclass
class CancelResult:
    appointment: Appointment
    approval: ApprovalRequest | None = None

    @property
    def cancelled(self) -> bool:
        return self.appointment.status is AppointmentStatus.CANCELLED


def appointment_snapshot(a: Appointment) -> dict[str, object]:
    return {
        "status": a.status.value,
        "patient_id": a.patient_id,
        "doctor_id": a.doctor_id,
        "type": a.type.value,
        "start_utc": a.start_utc.isoformat(),
        "end_utc": a.end_utc.isoformat(),
        "approval_reasons": list(a.approval_reasons or []),
        "rescheduled_from_id": a.rescheduled_from_id,
    }


def _book_inputs(_: object, actor: CurrentUser, cmd: BookCommand) -> dict[str, object]:
    return {
        "actor_role": actor.role,
        "patient_id": cmd.patient_id,
        "doctor_id": cmd.doctor_id,
        "slot_count": len(cmd.slot_ids),
        "type": cmd.type,
        "override": cmd.override,
    }


def _result_outputs(r: BookingResult | CancelResult) -> dict[str, object]:
    return {
        "appointment_id": r.appointment.id,
        "status": r.appointment.status,
        "approval_id": r.approval.id if r.approval else None,
        "approval_reasons": r.approval.reasons if r.approval else [],
    }


class BookingService:
    def __init__(self, session: Session, clock: Clock, settings: Settings) -> None:
        self.s = session
        self.clock = clock
        self.tz = settings.tz
        self.access = AccessPolicy(session)
        self.appts = AppointmentRepository(session)
        self.audit = AuditService(session)
        self.notify = NotificationService(session, settings.tz)

    # -- validation helpers ------------------------------------------------------------------
    def _load_slots(
        self, doctor_id: str, slot_ids: Sequence[str], appt_type: AppointmentType, now: datetime
    ) -> list[Slot]:
        if len(set(slot_ids)) != len(slot_ids):
            raise ValidationAppError("duplicate_slot_ids", "slot IDs must be distinct")
        needed = slots_needed(appt_type)
        if len(slot_ids) != needed:
            raise ValidationAppError(
                "wrong_slot_count",
                f"a {appt_type.value} visit needs {needed} consecutive slot(s)",
                details={"needed": needed},
            )
        slots = list(self.s.scalars(select(Slot).where(Slot.id.in_(slot_ids))))
        if len(slots) != len(slot_ids):
            raise ValidationAppError("slot_not_found", "one or more slots do not exist")
        slots.sort(key=lambda s: s.start_utc)
        if any(s.doctor_id != doctor_id for s in slots):
            raise ValidationAppError("slot_doctor_mismatch", "slots belong to a different doctor")
        if any(a.end_utc != b.start_utc for a, b in pairwise(slots)):
            raise ValidationAppError("slots_not_consecutive", "slots must be back-to-back")
        if slots[0].start_utc <= now:
            raise ValidationAppError("slot_in_past", "that time has already passed")
        held = self.s.scalar(
            select(
                exists().where(
                    AppointmentSlot.slot_id.in_([s.id for s in slots]), AppointmentSlot.active
                )
            )
        )
        if held or any(s.status is not SlotStatus.OPEN for s in slots):
            raise ConflictError("slot_unavailable", "that time is no longer available")
        return slots

    def _assert_no_duplicate(
        self,
        patient_id: str,
        doctor_id: str,
        start: datetime,
        end: datetime,
        exclude_ids: Sequence[str] = (),
    ) -> None:
        active = list(
            self.s.scalars(
                select(Appointment).where(
                    Appointment.patient_id == patient_id,
                    Appointment.status.in_(ACTIVE_STATUSES),
                    Appointment.id.not_in(exclude_ids) if exclude_ids else true(),
                )
            )
        )
        for a in active:
            if a.start_utc < end and start < a.end_utc:
                raise ConflictError(
                    "overlapping_appointment",
                    "the patient already has an appointment at that time",
                    details={"appointment_id": a.id},
                )
        day = start.astimezone(self.tz).date()
        for a in active:
            if a.doctor_id == doctor_id and a.start_utc.astimezone(self.tz).date() == day:
                raise ConflictError(
                    "duplicate_booking",
                    "the patient already has an appointment with this doctor that day",
                    details={"appointment_id": a.id},
                )

    def _is_new_patient(self, patient_id: str) -> bool:
        seen = {AppointmentStatus.COMPLETED, AppointmentStatus.CHECKED_IN}
        return not self.s.scalar(
            select(
                exists().where(Appointment.patient_id == patient_id, Appointment.status.in_(seen))
            )
        )

    def _context(
        self,
        actor: CurrentUser,
        patient: Patient,
        appt_type: AppointmentType,
        start: datetime,
        *,
        is_change: bool,
    ) -> BookingContext:
        today = clinic_today(self.clock, self.tz)
        return BookingContext(
            appt_start_utc=start,
            now_utc=self.clock.now_utc(),
            appointment_type=appt_type,
            is_change=is_change,
            is_new_patient=self._is_new_patient(patient.id),
            patient_is_minor=is_minor(patient.dob, today),
            booked_by_guardian=actor.role is Role.PATIENT and patient.user_id != actor.id,
            patient_no_show_flagged=patient.flagged,
        )

    def _doctor(self, doctor_id: str) -> Doctor:
        doctor = self.s.get(Doctor, doctor_id)
        if doctor is None:
            raise NotFoundError("doctor_not_found", "doctor not found")
        if not doctor.is_active:
            raise ValidationAppError("doctor_inactive", "this doctor is not taking appointments")
        return doctor

    def _hold(self, appt: Appointment, slots: Sequence[Slot]) -> None:
        try:
            with self.s.begin_nested():
                self.appts.hold_slots(appt, [s.id for s in slots])
        except IntegrityError as exc:
            raise ConflictError("slot_unavailable", "that time is no longer available") from exc

    def _insert(self, appt: Appointment) -> None:
        try:
            with self.s.begin_nested():
                self.s.add(appt)
                self.s.flush()
        except IntegrityError as exc:
            raise ConflictError("duplicate_request", "this request was already submitted") from exc

    def _request(self, **fields: object) -> ApprovalRequest:
        now = self.clock.now_utc()
        return ApprovalRequest(created_at=now, updated_at=now, **fields)

    def _pending_requests(self, appt_id: str) -> list[ApprovalRequest]:
        return list(
            self.s.scalars(
                select(ApprovalRequest).where(
                    ApprovalRequest.status == ApprovalStatus.PENDING,
                    (ApprovalRequest.appointment_id == appt_id)
                    | (ApprovalRequest.target_appointment_id == appt_id),
                )
            )
        )

    def _close_pending(self, appt: Appointment, actor: CurrentUser) -> None:
        """Withdraw requests about `appt`; a pending reschedule's proposed slot is released."""
        for req in self._pending_requests(appt.id):
            req.status = ApprovalStatus.WITHDRAWN
            req.decided_by_user_id = actor.id
            req.decided_at = self.clock.now_utc()
            req.decision_note = "superseded by cancellation"
            target_id = req.target_appointment_id
            if req.kind is ApprovalKind.RESCHEDULE and target_id and target_id != appt.id:
                target = self.s.get(Appointment, target_id)
                if target is not None and target.status is AppointmentStatus.PENDING_APPROVAL:
                    target.status = AppointmentStatus.CANCELLED
                    self.appts.release_slots(target.id)
            self.audit.record(actor, "approval.withdraw", "approval_request", req.id)
        self.s.flush()

    # -- booking -----------------------------------------------------------------------------
    @traced("booking.book", tags=["booking"], inputs=_book_inputs, outputs=_result_outputs)
    def book(self, actor: CurrentUser, cmd: BookCommand) -> BookingResult:
        if actor.role not in (Role.PATIENT, Role.FRONT_DESK, Role.ADMIN):
            raise ForbiddenError("forbidden", "your role cannot book appointments")
        if cmd.override and not actor.is_staff:
            raise ForbiddenError("override_forbidden", "only staff can override approval rules")
        if cmd.override and not (cmd.override_reason and cmd.override_reason.strip()):
            raise ValidationAppError("override_reason_required", "an override needs a reason")
        patient = self.access.assert_can_act_for_patient(actor, cmd.patient_id)
        doctor = self._doctor(cmd.doctor_id)
        now = self.clock.now_utc()
        slots = self._load_slots(doctor.id, cmd.slot_ids, cmd.type, now)
        start, end = slots[0].start_utc, slots[-1].end_utc
        self._assert_no_duplicate(patient.id, doctor.id, start, end)

        reasons = requires_approval(self._context(actor, patient, cmd.type, start, is_change=False))
        needs_approval = bool(reasons) and not cmd.override
        appt = Appointment(
            patient_id=patient.id,
            doctor_id=doctor.id,
            type=cmd.type,
            status=AppointmentStatus.REQUESTED,
            start_utc=start,
            end_utc=end,
            reason_text=cmd.reason,
            booked_by_user_id=actor.id,
            idempotency_key=cmd.idempotency_key,
            approval_reasons=[r.value for r in reasons],
        )
        event = (
            AppointmentEvent.SUBMIT_FOR_APPROVAL
            if needs_approval
            else AppointmentEvent.AUTO_CONFIRM
        )
        appt.status = transition(appt.status, event, _SYSTEM)
        self._insert(appt)
        self._hold(appt, slots)

        approval = None
        if needs_approval:
            approval = self._request(
                kind=ApprovalKind.NEW_BOOKING,
                appointment_id=appt.id,
                reasons=[r.value for r in reasons],
                requested_by_user_id=actor.id,
            )
            self.s.add(approval)
            self.s.flush()
        self.audit.record(
            actor, "appointment.create", "appointment", appt.id, after=appointment_snapshot(appt)
        )
        if cmd.override and reasons:
            self.audit.record(
                actor,
                "appointment.override",
                "appointment",
                appt.id,
                after={"skipped_reasons": [r.value for r in reasons]},
                reason=cmd.override_reason,
            )
        template = "appointment_pending_approval" if needs_approval else "appointment_confirmed"
        self.notify.for_appointment(appt, template)
        return BookingResult(appt, approval, [r.value for r in reasons])

    # -- cancellation ------------------------------------------------------------------------
    @traced(
        "booking.cancel",
        tags=["booking"],
        inputs=lambda _s, actor, appointment_id, *a, **k: {
            "actor_role": actor.role,
            "appointment_id": appointment_id,
        },
        outputs=_result_outputs,
    )
    def cancel(
        self, actor: CurrentUser, appointment_id: str, reason: str | None = None
    ) -> CancelResult:
        appt = self.access.get_appointment_for_view(actor, appointment_id)
        if actor.role is Role.DOCTOR:
            raise ForbiddenError("forbidden", "doctors cannot cancel appointments")
        transition(appt.status, AppointmentEvent.CANCEL, actor.role)  # legality check only
        now = self.clock.now_utc()
        before = appointment_snapshot(appt)

        if appt.status is AppointmentStatus.CONFIRMED:
            if appt.start_utc <= now:
                raise ConflictError("appointment_in_past", "this appointment has already started")
            if actor.role is Role.PATIENT and is_within_change_window(appt.start_utc, now):
                if self._pending_requests(appt.id):
                    raise ConflictError(
                        "change_already_requested",
                        "a change for this appointment is already pending",
                    )
                req = self._request(
                    kind=ApprovalKind.CANCEL,
                    appointment_id=appt.id,
                    reasons=[ApprovalReason.WITHIN_24H_CHANGE.value],
                    requested_by_user_id=actor.id,
                    request_note=reason,
                )
                self.s.add(req)
                self.s.flush()
                self.audit.record(
                    actor, "appointment.cancel_requested", "appointment", appt.id, reason=reason
                )
                self.notify.for_appointment(appt, "change_requested", change="cancel")
                return CancelResult(appt, req)

        self._close_pending(appt, actor)
        appt.status = transition(appt.status, AppointmentEvent.CANCEL, actor.role)
        self.appts.release_slots(appt.id)
        self.s.flush()
        self.audit.record(
            actor,
            "appointment.cancel",
            "appointment",
            appt.id,
            before=before,
            after=appointment_snapshot(appt),
            reason=reason,
        )
        self.notify.for_appointment(appt, "appointment_cancelled")
        return CancelResult(appt)

    # -- rescheduling ------------------------------------------------------------------------
    @traced(
        "booking.reschedule",
        tags=["booking"],
        inputs=lambda _s, actor, appointment_id, new_slot_ids, *a, **k: {
            "actor_role": actor.role,
            "appointment_id": appointment_id,
            "slot_count": len(new_slot_ids),
        },
        outputs=_result_outputs,
    )
    def reschedule(
        self,
        actor: CurrentUser,
        appointment_id: str,
        new_slot_ids: Sequence[str],
        reason: str | None = None,
        idempotency_key: str | None = None,
    ) -> BookingResult:
        old = self.access.get_appointment_for_view(actor, appointment_id)
        if actor.role is Role.DOCTOR:
            raise ForbiddenError("forbidden", "doctors cannot reschedule appointments")
        transition(old.status, AppointmentEvent.RESCHEDULE, actor.role)  # legality check only
        now = self.clock.now_utc()
        if old.start_utc <= now:
            raise ConflictError("appointment_in_past", "this appointment has already started")
        if self._pending_requests(old.id):
            raise ConflictError(
                "change_already_requested", "a change for this appointment is already pending"
            )
        patient = require(self.s.get(Patient, old.patient_id), "patient_not_found")
        self._doctor(old.doctor_id)
        slots = self._load_slots(old.doctor_id, new_slot_ids, old.type, now)
        start, end = slots[0].start_utc, slots[-1].end_utc
        self._assert_no_duplicate(patient.id, old.doctor_id, start, end, exclude_ids=[old.id])

        # Other approval reasons (minor, surgery consult, no-show flag) were vetted when the
        # original booking was confirmed; a move only re-triggers the 24h rule, and only for
        # patients (staff are the approvers).
        ctx = self._context(actor, patient, old.type, old.start_utc, is_change=True)
        reasons = [
            r
            for r in requires_approval(ctx)
            if r is ApprovalReason.WITHIN_24H_CHANGE and actor.role is Role.PATIENT
        ]
        new = Appointment(
            patient_id=old.patient_id,
            doctor_id=old.doctor_id,
            type=old.type,
            status=AppointmentStatus.REQUESTED,
            start_utc=start,
            end_utc=end,
            reason_text=reason or old.reason_text,
            booked_by_user_id=actor.id,
            idempotency_key=idempotency_key,
            rescheduled_from_id=old.id,
            approval_reasons=[r.value for r in reasons],
        )
        before_old = appointment_snapshot(old)
        approval = None
        if reasons:
            new.status = transition(new.status, AppointmentEvent.SUBMIT_FOR_APPROVAL, _SYSTEM)
            self._insert(new)
            self._hold(new, slots)
            approval = self._request(
                kind=ApprovalKind.RESCHEDULE,
                appointment_id=old.id,
                target_appointment_id=new.id,
                reasons=[r.value for r in reasons],
                requested_by_user_id=actor.id,
                request_note=reason,
            )
            self.s.add(approval)
            self.s.flush()
            self.audit.record(
                actor,
                "appointment.reschedule_requested",
                "appointment",
                old.id,
                after={"proposed_appointment_id": new.id, "reasons": [r.value for r in reasons]},
                reason=reason,
            )
            self.notify.for_appointment(old, "change_requested", change="reschedule")
        else:
            old.status = transition(old.status, AppointmentEvent.RESCHEDULE, actor.role)
            self.appts.release_slots(old.id)
            new.status = transition(new.status, AppointmentEvent.AUTO_CONFIRM, _SYSTEM)
            self._insert(new)
            self._hold(new, slots)
            self.audit.record(
                actor,
                "appointment.reschedule",
                "appointment",
                old.id,
                before=before_old,
                after={**appointment_snapshot(old), "new_appointment_id": new.id},
                reason=reason,
            )
            self.notify.for_appointment(new, "appointment_rescheduled")
        self.audit.record(
            actor, "appointment.create", "appointment", new.id, after=appointment_snapshot(new)
        )
        return BookingResult(new, approval, [r.value for r in reasons])

    # -- visit day ---------------------------------------------------------------------------
    def _staff_only(self, actor: CurrentUser) -> None:
        if not actor.is_staff:
            raise ForbiddenError("forbidden", "front desk only")

    def check_in(self, actor: CurrentUser, appointment_id: str) -> Appointment:
        self._staff_only(actor)
        appt = self.access.get_appointment_for_view(actor, appointment_id)
        now = self.clock.now_utc()
        new_status = transition(appt.status, AppointmentEvent.CHECK_IN, actor.role)
        if appt.start_utc.astimezone(self.tz).date() != now.astimezone(self.tz).date():
            raise ConflictError(
                "check_in_window", "check-in is only possible on the appointment day"
            )
        if now > appt.end_utc + CHECK_IN_GRACE:
            raise ConflictError("check_in_window", "too late to check in; mark as no-show instead")
        before = appointment_snapshot(appt)
        appt.status = new_status
        self.s.flush()
        self.audit.record(
            actor,
            "appointment.check_in",
            "appointment",
            appt.id,
            before=before,
            after=appointment_snapshot(appt),
        )
        return appt

    def mark_no_show(self, actor: CurrentUser, appointment_id: str) -> Appointment:
        self._staff_only(actor)
        appt = self.access.get_appointment_for_view(actor, appointment_id)
        new_status = transition(appt.status, AppointmentEvent.MARK_NO_SHOW, actor.role)
        if self.clock.now_utc() < appt.end_utc:
            raise ConflictError("too_early", "a no-show can only be recorded after the slot ends")
        before = appointment_snapshot(appt)
        appt.status = new_status
        self.appts.release_slots(appt.id)
        patient = require(self.s.get(Patient, appt.patient_id), "patient_not_found")
        patient.no_show_count += 1
        newly_flagged = not patient.flagged and should_flag_for_no_shows(patient.no_show_count)
        patient.flagged = patient.flagged or newly_flagged
        self.s.flush()
        self.audit.record(
            actor,
            "appointment.no_show",
            "appointment",
            appt.id,
            before=before,
            after={
                **appointment_snapshot(appt),
                "no_show_count": patient.no_show_count,
                "patient_flagged": patient.flagged,
            },
        )
        if newly_flagged:
            self.audit.record(
                actor,
                "patient.flagged",
                "patient",
                patient.id,
                after={"no_show_count": patient.no_show_count},
            )
        self.notify.for_appointment(appt, "no_show_recorded")
        return appt

    def complete(self, actor: CurrentUser, appointment_id: str) -> Appointment:
        appt = self.access.get_appointment_for_view(actor, appointment_id)  # doctor: own only
        if actor.role is Role.PATIENT:
            raise ForbiddenError("forbidden", "patients cannot complete visits")
        before = appointment_snapshot(appt)
        appt.status = transition(appt.status, AppointmentEvent.COMPLETE, actor.role)
        self.s.flush()
        self.audit.record(
            actor,
            "appointment.complete",
            "appointment",
            appt.id,
            before=before,
            after=appointment_snapshot(appt),
        )
        return appt
