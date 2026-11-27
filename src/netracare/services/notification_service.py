"""Simulated notifications: rows in `notification_outbox` (nothing is actually sent)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from netracare.db.models import Appointment, Doctor, NotificationOutbox, Patient
from netracare.domain.actor import CurrentUser
from netracare.domain.enums import NotificationChannel, Role
from netracare.errors import ForbiddenError, NotFoundError
from netracare.services.access import AccessPolicy

# Patient-facing copy. Never includes clinical advice; times are clinic-local.
TEMPLATES: dict[str, str] = {
    "appointment_confirmed": "Your appointment with {doctor} on {when} is confirmed.",
    "appointment_pending_approval": (
        "We received your request for {doctor} on {when}. Our front desk will confirm it shortly."
    ),
    "appointment_cancelled": "Your appointment with {doctor} on {when} has been cancelled.",
    "appointment_rescheduled": "Your appointment with {doctor} has moved to {when}.",
    "change_requested": (
        "We received your request to {change} your appointment with {doctor} on {when}. "
        "Our front desk will review it."
    ),
    "approval_approved": "Good news: your request for {doctor} on {when} was approved.",
    "approval_changed": (
        "Your request was confirmed at a different time: {doctor} on {when}. Contact the front "
        "desk if that does not suit you."
    ),
    "approval_overdue": (
        "Our team hasn't confirmed your request for {doctor} on {when} yet; we'll contact you, "
        "or you can call +91 80 0000 1234."
    ),
    "approval_rejected": (
        "Your request for {doctor} on {when} could not be approved. Please contact the front desk."
    ),
    "leave_conflict": (
        "{doctor} is unavailable on {when}. Our front desk will contact you to reschedule."
    ),
    "holiday_conflict": (
        "The clinic is closed on {when}. Our front desk will contact you to reschedule."
    ),
    "no_show_recorded": "We missed you for your appointment with {doctor} on {when}.",
    "visit_summary_ready": (
        "{doctor} has shared a summary of your visit on {when}. You can read it under "
        "My appointments."
    ),
}


@dataclass(frozen=True)
class OutboxPage:
    items: list[NotificationOutbox]
    total: int


class NotificationService:
    def __init__(self, session: Session, tz: ZoneInfo) -> None:
        self.s = session
        self.tz = tz

    def enqueue(
        self, patient_id: str, template: str, payload: dict[str, Any]
    ) -> NotificationOutbox:
        if template not in TEMPLATES:
            raise ValueError(f"unknown notification template: {template}")
        patient = self.s.get(Patient, patient_id)
        if patient is None:
            raise NotFoundError("patient_not_found")
        channel = NotificationChannel.SMS if patient.phone else NotificationChannel.EMAIL
        body = TEMPLATES[template].format_map(_Default(payload))
        row = NotificationOutbox(
            channel=channel,
            to_patient_id=patient_id,
            template=template,
            payload_json={**payload, "body": body},
        )
        self.s.add(row)
        self.s.flush()
        return row

    def for_appointment(self, appt: Appointment, template: str, **extra: Any) -> NotificationOutbox:
        doctor = self.s.get(Doctor, appt.doctor_id)
        when = appt.start_utc.astimezone(self.tz).strftime("%a %d %b %Y, %I:%M %p")
        payload = {
            "appointment_id": appt.id,
            "doctor": doctor.full_name if doctor else "your doctor",
            "when": when,
            **extra,
        }
        return self.enqueue(appt.patient_id, template, payload)

    def list_for(
        self, user: CurrentUser, patient_id: str | None, limit: int, offset: int
    ) -> OutboxPage:
        stmt = select(NotificationOutbox)
        if user.role is Role.DOCTOR:
            raise ForbiddenError("forbidden", "doctors do not have access to the outbox")
        if user.role is Role.PATIENT:
            allowed = AccessPolicy(self.s).patient_ids_for(user)
            if patient_id is not None and patient_id not in allowed:
                raise NotFoundError("patient_not_found", "patient not found")
            stmt = stmt.where(
                NotificationOutbox.to_patient_id.in_([patient_id] if patient_id else allowed)
            )
        elif patient_id is not None:
            stmt = stmt.where(NotificationOutbox.to_patient_id == patient_id)
        total = int(self.s.scalar(select(func.count()).select_from(stmt.subquery())) or 0)
        rows = self.s.scalars(
            stmt.order_by(NotificationOutbox.created_at.desc(), NotificationOutbox.id)
            .limit(limit)
            .offset(offset)
        )
        return OutboxPage(list(rows), total)


class _Default(dict[str, Any]):
    def __missing__(self, key: str) -> str:
        return "{" + key + "}"
