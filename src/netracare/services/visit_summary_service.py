"""Visit-summary drafts: doctor notes -> patient-friendly summary that only the doctor can
release.

* Doctor (own appointment, checked in or completed) creates a draft from notes. The patient's
  name, phone and email are redacted before the LLM sees the notes.
* Deterministic faithfulness checks (`check_summary`) produce `needs_attention` flags; approval
  is blocked until every flag is edited away or acknowledged.
* Patients (owner/guardian) only ever see an *approved* summary; anything else is 404, so the
  existence of a draft never leaks. Staff see status only. Notes never go to logs or Opik.
* LLM unavailable -> 503 `llm_unavailable`; the notes are kept as an empty draft so the doctor
  can write the summary manually (PATCH) and approve it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session
from sqlalchemy.orm.exc import StaleDataError

from netracare.config import Settings
from netracare.db.models import Appointment, Doctor, Patient, PatientGuardian, User, VisitSummary
from netracare.db.session import outside_transaction
from netracare.domain.actor import CurrentUser
from netracare.domain.clock import Clock
from netracare.domain.enums import AppointmentStatus, Role, SummaryStatus
from netracare.errors import (
    ConflictError,
    NotFoundError,
    ServiceUnavailableError,
    ValidationAppError,
)
from netracare.guardrails.advice_filter import check_summary
from netracare.llm.client import LLMClient
from netracare.llm.schemas import VisitSummaryDraft
from netracare.llm.types import LLMError
from netracare.log import get_logger
from netracare.observability.tracing import traced
from netracare.services.access import AccessPolicy
from netracare.services.audit_service import AuditService
from netracare.services.notification_service import NotificationService

log = get_logger(__name__)

MIN_NOTES, MAX_NOTES = 20, 8000
_VISIT_STATUSES = frozenset({AppointmentStatus.CHECKED_IN, AppointmentStatus.COMPLETED})
EMPTY_DRAFT: dict[str, Any] = {
    "what_we_checked": [],
    "what_we_found": "",
    "next_steps": [],
    "medicines_mentioned": [],
    "follow_up": None,
    "unclear_in_notes": [],
}


@dataclass(frozen=True)
class SummaryView:
    appointment_id: str
    status: SummaryStatus
    audience: str  # doctor | patient | staff
    draft: dict[str, Any] | None = None
    final: dict[str, Any] | None = None
    notes: str | None = None
    needs_attention: tuple[str, ...] = ()
    acknowledged_flags: tuple[str, ...] = ()
    unresolved_flags: tuple[str, ...] = ()
    doctor_name: str | None = None
    approved_at: datetime | None = None
    prompt_ref: str | None = None
    version: int = 1


def _draft_inputs(_: object, actor: CurrentUser, appointment_id: str, notes: str) -> dict[str, Any]:
    return {"appointment_id": appointment_id, "notes_len": len(notes)}


def _view_outputs(v: SummaryView) -> dict[str, Any]:
    return {
        "status": v.status,
        "flags": len(v.needs_attention),
        "unresolved": len(v.unresolved_flags),
    }


class VisitSummaryService:
    def __init__(
        self, session: Session, clock: Clock, settings: Settings, llm: LLMClient | None
    ) -> None:
        self.s = session
        self.clock = clock
        self.settings = settings
        self.llm = llm
        self.access = AccessPolicy(session)
        self.audit = AuditService(session)
        self.notify = NotificationService(session, settings.tz)

    # ------------------------------------------------------------------ helpers

    def _own_appointment(self, actor: CurrentUser, appointment_id: str) -> Appointment:
        appt = self.s.get(Appointment, appointment_id)
        if appt is None or actor.role is not Role.DOCTOR or appt.doctor_id != actor.doctor_id:
            raise NotFoundError("appointment_not_found", "appointment not found")
        return appt

    def _summary(self, appointment_id: str) -> VisitSummary | None:
        return self.s.scalar(
            select(VisitSummary).where(VisitSummary.appointment_id == appointment_id)
        )

    def _require_summary(self, appointment_id: str) -> VisitSummary:
        summary = self._summary(appointment_id)
        if summary is None:
            raise NotFoundError("summary_not_found", "no visit summary for this appointment")
        return summary

    def _redaction_names(self, appt: Appointment) -> list[str]:
        patient = self.s.get(Patient, appt.patient_id)
        if patient is None:  # pragma: no cover - FK guarantees it
            return []
        names = [patient.full_name]
        names += [v for v in (patient.phone, patient.email) if v]
        guardian_emails = self.s.scalars(
            select(User.email)
            .join(PatientGuardian, PatientGuardian.guardian_user_id == User.id)
            .where(PatientGuardian.patient_id == patient.id)
        )
        names += list(guardian_emails)
        return names

    def _flush(self) -> None:
        try:
            self.s.flush()
        except StaleDataError as exc:  # pragma: no cover - needs a true concurrent writer
            raise ConflictError(
                "summary_changed", "the summary was changed by someone else"
            ) from exc

    def _doctor_view(self, summary: VisitSummary) -> SummaryView:
        unresolved = tuple(
            f for f in summary.needs_attention if f not in summary.acknowledged_flags
        )
        ref = f"{summary.prompt_id}@v{summary.prompt_version}" if summary.prompt_id else None
        return SummaryView(
            appointment_id=summary.appointment_id,
            status=summary.status,
            audience="doctor",
            draft=summary.draft_json,
            final=summary.final_json,
            notes=summary.raw_notes,
            needs_attention=tuple(summary.needs_attention),
            acknowledged_flags=tuple(summary.acknowledged_flags),
            unresolved_flags=unresolved,
            doctor_name=self._doctor_name(summary.doctor_id),
            approved_at=summary.approved_at,
            prompt_ref=ref,
            version=summary.version,
        )

    def _doctor_name(self, doctor_id: str) -> str | None:
        doctor = self.s.get(Doctor, doctor_id)
        return doctor.full_name if doctor else None

    # ------------------------------------------------------------------ doctor actions

    @traced(
        "visit_summary.draft", tags=["visit_summary"], inputs=_draft_inputs, outputs=_view_outputs
    )
    def create_draft(self, actor: CurrentUser, appointment_id: str, notes: str) -> SummaryView:
        appt = self._own_appointment(actor, appointment_id)
        if appt.status not in _VISIT_STATUSES:
            raise ConflictError(
                "visit_not_started", "summaries can be written once the patient has checked in"
            )
        notes = notes.strip()
        if not MIN_NOTES <= len(notes) <= MAX_NOTES:
            raise ValidationAppError(
                "notes_length", f"notes must be {MIN_NOTES}-{MAX_NOTES} characters"
            )
        existing = self._summary(appointment_id)
        if existing is not None and existing.status is SummaryStatus.APPROVED:
            raise ConflictError("already_approved", "this summary is already approved")
        names = self._redaction_names(appt)

        draft: dict[str, Any] | None = None
        prompt_id = prompt_version = model = None
        error: LLMError | None = None
        if self.llm is None or not self.llm.available:
            error = LLMError("LLM unavailable")
        else:
            try:
                with outside_transaction(self.s):
                    result = self.llm.generate(
                        "visit_summary",
                        {"notes": notes},
                        purpose="visit_summary",
                        schema=VisitSummaryDraft,
                        actor_user_id=actor.id,
                        redact_names=names,
                    )
                draft = result.value.model_dump()
                prompt_id, prompt_version, model = (
                    result.prompt_id,
                    result.prompt_version,
                    result.model,
                )
            except LLMError as exc:
                error = exc
        # Re-read after the LLM call: the transaction was committed and reopened.
        existing = self._summary(appointment_id)
        if existing is not None and existing.status is SummaryStatus.APPROVED:
            raise ConflictError("already_approved", "this summary is already approved")

        summary = existing or VisitSummary(appointment_id=appt.id, doctor_id=appt.doctor_id)
        summary.raw_notes = notes
        summary.draft_json = draft if draft is not None else dict(EMPTY_DRAFT)
        summary.status = SummaryStatus.DRAFT
        summary.needs_attention = check_summary(summary.draft_json, notes) if draft else []
        summary.acknowledged_flags = []
        summary.prompt_id, summary.prompt_version, summary.model = prompt_id, prompt_version, model
        now = self.clock.now_utc()
        summary.updated_at = now
        if existing is None:
            summary.created_at = now
            self.s.add(summary)
        self._flush()
        self.audit.record(
            actor,
            "visit_summary.draft",
            "visit_summary",
            summary.id,
            after={
                "appointment_id": appt.id,
                "source": "llm" if draft else "manual",
                "flags": len(summary.needs_attention),
                "prompt": f"{prompt_id}@v{prompt_version}" if prompt_id else None,
            },
        )
        if error is not None:
            log.warning("visit_summary.llm_unavailable", error=error.code)
            raise ServiceUnavailableError(
                "llm_unavailable",
                "AI drafting is unavailable right now. Your notes are saved: please write the "
                "summary manually and approve it.",
                details={"manual_draft": True},
                commit=True,
            )
        return self._doctor_view(summary)

    def edit(
        self,
        actor: CurrentUser,
        appointment_id: str,
        draft: dict[str, Any],
        acknowledged_flags: list[str],
    ) -> SummaryView:
        self._own_appointment(actor, appointment_id)
        summary = self._require_summary(appointment_id)
        if summary.status is SummaryStatus.APPROVED:
            raise ConflictError("already_approved", "this summary is already approved")
        try:
            validated = VisitSummaryDraft.model_validate(draft).model_dump()
        except ValidationError as exc:
            raise ValidationAppError("invalid_summary", "the summary is not valid") from exc
        summary.draft_json = validated
        summary.needs_attention = check_summary(validated, summary.raw_notes)
        summary.acknowledged_flags = [f for f in acknowledged_flags if f in summary.needs_attention]
        summary.status = SummaryStatus.DRAFT
        summary.updated_at = self.clock.now_utc()
        self._flush()
        self.audit.record(
            actor,
            "visit_summary.edit",
            "visit_summary",
            summary.id,
            after={
                "flags": len(summary.needs_attention),
                "acknowledged": len(summary.acknowledged_flags),
            },
        )
        return self._doctor_view(summary)

    def approve(self, actor: CurrentUser, appointment_id: str) -> SummaryView:
        appt = self._own_appointment(actor, appointment_id)
        summary = self._require_summary(appointment_id)
        if summary.status is SummaryStatus.APPROVED:
            raise ConflictError("already_approved", "this summary is already approved")
        if summary.status is SummaryStatus.DISCARDED:
            raise ConflictError("summary_discarded", "this draft was discarded")
        if not (summary.draft_json.get("what_we_found") or "").strip():
            raise ValidationAppError("empty_summary", "write what was found before approving")
        unresolved = [f for f in summary.needs_attention if f not in summary.acknowledged_flags]
        if unresolved:
            raise ConflictError(
                "unresolved_flags",
                "edit or acknowledge every flagged item before approving",
                details={"unresolved": unresolved},
            )
        now = self.clock.now_utc()
        summary.status = SummaryStatus.APPROVED
        summary.final_json = dict(summary.draft_json)
        summary.approved_by = actor.id
        summary.approved_at = now
        summary.updated_at = now
        self._flush()
        self.audit.record(
            actor,
            "visit_summary.approve",
            "visit_summary",
            summary.id,
            after={"appointment_id": appt.id, "acknowledged": len(summary.acknowledged_flags)},
        )
        self.notify.for_appointment(appt, "visit_summary_ready")
        return self._doctor_view(summary)

    def discard(self, actor: CurrentUser, appointment_id: str) -> SummaryView:
        self._own_appointment(actor, appointment_id)
        summary = self._require_summary(appointment_id)
        if summary.status is SummaryStatus.APPROVED:
            raise ConflictError("already_approved", "an approved summary cannot be discarded")
        summary.status = SummaryStatus.DISCARDED
        summary.updated_at = self.clock.now_utc()
        self._flush()
        self.audit.record(actor, "visit_summary.discard", "visit_summary", summary.id)
        return self._doctor_view(summary)

    # ------------------------------------------------------------------ reads

    def get_for(self, actor: CurrentUser, appointment_id: str) -> SummaryView:
        if actor.role is Role.DOCTOR:
            self._own_appointment(actor, appointment_id)
            return self._doctor_view(self._require_summary(appointment_id))
        appt = self.access.get_appointment_for_view(actor, appointment_id)  # 404 if not theirs
        summary = self._summary(appt.id)
        if actor.is_staff:
            if summary is None:
                raise NotFoundError("summary_not_found", "no visit summary for this appointment")
            return SummaryView(
                appointment_id=appt.id,
                status=summary.status,
                audience="staff",
                doctor_name=self._doctor_name(summary.doctor_id),
                approved_at=summary.approved_at,
                version=summary.version,
            )
        # Patients and guardians: approved summaries only; drafts do not exist for them.
        if summary is None or summary.status is not SummaryStatus.APPROVED:
            raise NotFoundError("summary_not_found", "no visit summary for this appointment")
        return SummaryView(
            appointment_id=appt.id,
            status=summary.status,
            audience="patient",
            final=summary.final_json,
            doctor_name=self._doctor_name(summary.doctor_id),
            approved_at=summary.approved_at,
            version=summary.version,
        )
