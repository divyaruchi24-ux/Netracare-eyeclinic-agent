"""Front-desk approval queue (human-in-the-loop). Reused by the Phase 4 booking agent."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session
from sqlalchemy.orm.exc import StaleDataError

from netracare.config import Settings
from netracare.db.models import Appointment, ApprovalRequest
from netracare.domain.actor import CurrentUser
from netracare.domain.clock import Clock
from netracare.domain.enums import (
    AppointmentEvent,
    ApprovalKind,
    ApprovalStatus,
    Role,
)
from netracare.domain.state_machine import transition
from netracare.errors import ConflictError, ForbiddenError, NotFoundError, require
from netracare.observability.tracing import traced
from netracare.repositories.appointment_repo import AppointmentRepository
from netracare.services.access import AccessPolicy
from netracare.services.audit_service import AuditService
from netracare.services.booking_service import appointment_snapshot
from netracare.services.notification_service import NotificationService

OVERDUE_AFTER = timedelta(hours=4)


@dataclass(frozen=True)
class ApprovalView:
    request: ApprovalRequest
    appointment: Appointment
    target: Appointment | None
    is_overdue: bool


def _decision_inputs(
    _: object, actor: CurrentUser, request_id: str, *a: object, **k: object
) -> dict[str, object]:
    return {"actor_role": actor.role, "request_id": request_id}


def _decision_outputs(v: ApprovalView) -> dict[str, object]:
    return {
        "kind": v.request.kind,
        "status": v.request.status,
        "appointment_status": v.appointment.status,
        "target_status": v.target.status if v.target else None,
    }


class ApprovalService:
    def __init__(self, session: Session, clock: Clock, settings: Settings) -> None:
        self.s = session
        self.clock = clock
        self.appts = AppointmentRepository(session)
        self.audit = AuditService(session)
        self.notify = NotificationService(session, settings.tz)

    def _view(self, req: ApprovalRequest) -> ApprovalView:
        appt = require(self.s.get(Appointment, req.appointment_id), "appointment_not_found")
        target = (
            self.s.get(Appointment, req.target_appointment_id)
            if req.target_appointment_id
            else None
        )
        overdue = (
            req.status is ApprovalStatus.PENDING
            and self.clock.now_utc() - req.created_at > OVERDUE_AFTER
        )
        return ApprovalView(req, appt, target, overdue)

    def list(
        self, status: ApprovalStatus | None = ApprovalStatus.PENDING, limit: int = 100
    ) -> list[ApprovalView]:
        stmt = select(ApprovalRequest).order_by(ApprovalRequest.created_at).limit(limit)
        if status is not None:
            stmt = stmt.where(ApprovalRequest.status == status)
        views = [self._view(r) for r in self.s.scalars(stmt)]
        return sorted(views, key=lambda v: (not v.is_overdue, v.request.created_at))

    def get(self, actor: CurrentUser, request_id: str) -> ApprovalView:
        req = self.s.get(ApprovalRequest, request_id)
        if req is None:
            raise NotFoundError("approval_not_found", "approval request not found")
        if not actor.is_staff:
            AccessPolicy(self.s).get_appointment_for_view(actor, req.appointment_id)
        return self._view(req)

    def _pending(self, request_id: str) -> ApprovalRequest:
        req = self.s.get(ApprovalRequest, request_id)
        if req is None:
            raise NotFoundError("approval_not_found", "approval request not found")
        if req.status is not ApprovalStatus.PENDING:
            raise ConflictError(
                "already_decided",
                f"this request was already {req.status.value}",
                details={"status": req.status.value},
            )
        return req

    def _decide(
        self, req: ApprovalRequest, actor: CurrentUser, status: ApprovalStatus, note: str | None
    ) -> None:
        req.status = status
        req.decided_by_user_id = actor.id
        req.decided_at = self.clock.now_utc()
        req.decision_note = note
        try:
            self.s.flush()
        except StaleDataError as exc:  # someone else decided first
            raise ConflictError(
                "already_decided", "this request was decided by someone else"
            ) from exc

    def _staff(self, actor: CurrentUser) -> None:
        if not actor.is_staff:
            raise ForbiddenError("forbidden", "only the front desk can decide approvals")

    @traced(
        "approval.approve", tags=["approval"], inputs=_decision_inputs, outputs=_decision_outputs
    )
    def approve(self, actor: CurrentUser, request_id: str, note: str | None = None) -> ApprovalView:
        self._staff(actor)
        req = self._pending(request_id)
        view = self._view(req)
        appt, target = view.appointment, view.target
        starts = (target or appt).start_utc
        if req.kind is not ApprovalKind.CANCEL and starts <= self.clock.now_utc():
            raise ConflictError("appointment_in_past", "the requested time has already passed")
        before = appointment_snapshot(appt)
        if req.kind is ApprovalKind.NEW_BOOKING:
            appt.status = transition(appt.status, AppointmentEvent.APPROVE, actor.role)
            notify_appt = appt
        elif req.kind is ApprovalKind.RESCHEDULE:
            target = require(target, "appointment_not_found")
            appt.status = transition(appt.status, AppointmentEvent.RESCHEDULE, actor.role)
            self.appts.release_slots(appt.id)
            target.status = transition(target.status, AppointmentEvent.APPROVE, actor.role)
            notify_appt = target
        else:
            appt.status = transition(appt.status, AppointmentEvent.CANCEL, actor.role)
            self.appts.release_slots(appt.id)
            notify_appt = appt
        self._decide(req, actor, ApprovalStatus.APPROVED, note)
        self.audit.record(
            actor,
            "approval.approve",
            "approval_request",
            req.id,
            before=before,
            after={
                "kind": req.kind.value,
                "appointment_status": appt.status.value,
                "target_status": target.status.value if target else None,
            },
            reason=note,
        )
        template = (
            "appointment_cancelled" if req.kind is ApprovalKind.CANCEL else "approval_approved"
        )
        self.notify.for_appointment(notify_appt, template)
        return self._view(req)

    @traced(
        "approval.reject", tags=["approval"], inputs=_decision_inputs, outputs=_decision_outputs
    )
    def reject(self, actor: CurrentUser, request_id: str, note: str | None = None) -> ApprovalView:
        self._staff(actor)
        req = self._pending(request_id)
        view = self._view(req)
        appt, target = view.appointment, view.target
        if req.kind is ApprovalKind.NEW_BOOKING:
            appt.status = transition(appt.status, AppointmentEvent.REJECT, actor.role)
            self.appts.release_slots(appt.id)
            notify_appt = appt
        elif req.kind is ApprovalKind.RESCHEDULE:
            target = require(target, "appointment_not_found")
            target.status = transition(target.status, AppointmentEvent.REJECT, actor.role)
            self.appts.release_slots(target.id)
            notify_appt = target  # the original appointment stays confirmed
        else:
            notify_appt = appt  # cancellation refused: appointment stays confirmed
        self._decide(req, actor, ApprovalStatus.REJECTED, note)
        self.audit.record(
            actor,
            "approval.reject",
            "approval_request",
            req.id,
            after={
                "kind": req.kind.value,
                "appointment_status": appt.status.value,
                "target_status": target.status.value if target else None,
            },
            reason=note,
        )
        self.notify.for_appointment(notify_appt, "approval_rejected")
        return self._view(req)

    def withdraw(self, actor: CurrentUser, request_id: str) -> ApprovalView:
        if actor.role is not Role.PATIENT:
            raise ForbiddenError("forbidden", "only the requesting patient can withdraw")
        req = self._pending(request_id)
        AccessPolicy(self.s).get_appointment_for_view(
            actor, req.appointment_id
        )  # 404 if not theirs
        view = self._view(req)
        if req.kind is ApprovalKind.NEW_BOOKING:
            view.appointment.status = transition(
                view.appointment.status, AppointmentEvent.CANCEL, actor.role
            )
            self.appts.release_slots(view.appointment.id)
        elif req.kind is ApprovalKind.RESCHEDULE and view.target is not None:
            view.target.status = transition(view.target.status, AppointmentEvent.CANCEL, actor.role)
            self.appts.release_slots(view.target.id)
        self._decide(req, actor, ApprovalStatus.WITHDRAWN, None)
        self.audit.record(
            actor, "approval.withdraw", "approval_request", req.id, after={"kind": req.kind.value}
        )
        return self._view(req)
