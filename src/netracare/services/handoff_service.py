"""Front-desk takeover of a chat (safety policy section 12).

A handoff carries a PII-redacted context summary (the last few turns), so staff see what the
patient needs without the summary itself spreading names, emails or phone numbers. The full
conversation is readable by front desk / admin only through the handoff.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session
from sqlalchemy.orm.exc import StaleDataError

from netracare.db.models import ChatHandoff, ChatMessage, ChatSession, Patient
from netracare.domain.actor import CurrentUser
from netracare.domain.clock import Clock
from netracare.domain.enums import ChatRole, ChatSessionStatus, HandoffReason, HandoffStatus
from netracare.errors import ConflictError, NotFoundError, ValidationAppError
from netracare.log import get_logger, redact_all
from netracare.services.audit_service import AuditService

log = get_logger(__name__)

SUMMARY_TURNS = 6
SUMMARY_LINE_CHARS = 300

REASON_LABELS: dict[HandoffReason, str] = {
    HandoffReason.USER_REQUEST: "Patient asked for a person",
    HandoffReason.LOW_CONFIDENCE: "Assistant could not answer reliably",
    HandoffReason.ROUTER_UNCERTAIN: "Assistant was unsure what the patient needs",
    HandoffReason.FRUSTRATION: "Patient seems frustrated with the assistant",
    HandoffReason.SPAM: "Repeated identical messages (session ended)",
    HandoffReason.BUDGET: "Conversation reached its AI budget",
}
_ACTIVE = (HandoffStatus.OPEN, HandoffStatus.CLAIMED)


class HandoffService:
    def __init__(self, session: Session, clock: Clock) -> None:
        self.s = session
        self.clock = clock
        self.audit = AuditService(session)

    def _names(self, chat: ChatSession) -> list[str]:
        if chat.patient_id is None:
            return []
        name = self.s.scalar(select(Patient.full_name).where(Patient.id == chat.patient_id))
        return [name] if name else []

    def summary(self, chat: ChatSession, reason: HandoffReason) -> str:
        msgs = list(
            self.s.scalars(
                select(ChatMessage)
                .where(ChatMessage.session_id == chat.id)
                .order_by(ChatMessage.seq.desc())
                .limit(SUMMARY_TURNS)
            )
        )[::-1]
        names = self._names(chat)
        lines = [f"Reason: {REASON_LABELS[reason]}"]
        for m in msgs:
            who = "Patient" if m.role is ChatRole.USER else "Assistant"
            text = redact_all(" ".join(m.text.split()), names)
            if len(text) > SUMMARY_LINE_CHARS:
                text = text[: SUMMARY_LINE_CHARS - 1] + "…"
            lines.append(f"{who}: {text}")
        return "\n".join(lines)

    def open_for(self, chat_id: str) -> ChatHandoff | None:
        return self.s.scalar(
            select(ChatHandoff)
            .where(ChatHandoff.session_id == chat_id, ChatHandoff.status.in_(_ACTIVE))
            .order_by(ChatHandoff.created_at.desc())
            .limit(1)
        )

    def create(
        self, chat: ChatSession, reason: HandoffReason, actor: CurrentUser | None = None
    ) -> ChatHandoff:
        """Idempotent per session: an open or claimed handoff is returned as it is."""
        existing = self.open_for(chat.id)
        if existing is not None:
            return existing
        handoff = ChatHandoff(
            session_id=chat.id,
            reason=reason,
            summary_redacted=self.summary(chat, reason),
            status=HandoffStatus.OPEN,
            created_at=self.clock.now_utc(),
        )
        self.s.add(handoff)
        if reason is not HandoffReason.SPAM:
            chat.status = ChatSessionStatus.HANDOFF
        self.s.flush()
        self.audit.record(
            actor, "chat.handoff", "chat_handoff", handoff.id, after={"reason": reason.value}
        )
        log.info("chat.handoff", reason=reason.value, handoff_id=handoff.id)
        return handoff

    # -- staff ------------------------------------------------------------------------------
    def list_handoffs(
        self, status: HandoffStatus | None = None, limit: int = 100
    ) -> list[ChatHandoff]:
        stmt = select(ChatHandoff)
        if status is not None:
            stmt = stmt.where(ChatHandoff.status == status)
        order = {HandoffStatus.OPEN: 0, HandoffStatus.CLAIMED: 1, HandoffStatus.CLOSED: 2}
        rows = list(self.s.scalars(stmt.order_by(ChatHandoff.created_at.desc()).limit(limit)))
        return sorted(rows, key=lambda h: (order[h.status], -h.created_at.timestamp()))

    def get(self, handoff_id: str) -> ChatHandoff:
        h = self.s.get(ChatHandoff, handoff_id)
        if h is None:
            raise NotFoundError("handoff_not_found", "handoff not found")
        return h

    def claim(
        self, actor: CurrentUser, handoff_id: str, expected_version: int | None = None
    ) -> ChatHandoff:
        h = self.get(handoff_id)
        if h.status is not HandoffStatus.OPEN or (
            expected_version is not None and h.version != expected_version
        ):
            raise ConflictError("already_handled", "this handoff was already picked up")
        h.status = HandoffStatus.CLAIMED
        h.claimed_by = actor.id
        h.claimed_at = self.clock.now_utc()
        self._flush()
        self.audit.record(actor, "chat.handoff_claim", "chat_handoff", h.id)
        return h

    def close(
        self, actor: CurrentUser, handoff_id: str, note: str, expected_version: int | None = None
    ) -> ChatHandoff:
        if not note.strip():
            raise ValidationAppError("note_required", "a closing note is required")
        h = self.get(handoff_id)
        if h.status is HandoffStatus.CLOSED or (
            expected_version is not None and h.version != expected_version
        ):
            raise ConflictError("already_handled", "this handoff was already closed")
        if h.status is HandoffStatus.OPEN:
            h.claimed_by = actor.id
            h.claimed_at = self.clock.now_utc()
        h.status = HandoffStatus.CLOSED
        h.closed_by = actor.id
        h.closed_at = self.clock.now_utc()
        h.close_note = note.strip()[:1000]
        chat = self.s.get(ChatSession, h.session_id)
        if chat is not None and chat.status is ChatSessionStatus.HANDOFF:
            chat.status = ChatSessionStatus.ACTIVE
        self._flush()
        self.audit.record(actor, "chat.handoff_close", "chat_handoff", h.id, reason=note[:200])
        return h

    def conversation(self, handoff_id: str) -> tuple[ChatHandoff, ChatSession, list[ChatMessage]]:
        h = self.get(handoff_id)
        chat = self.s.get(ChatSession, h.session_id)
        if chat is None:  # pragma: no cover - FK guarantees it
            raise NotFoundError("chat_not_found", "conversation not found")
        msgs = list(
            self.s.scalars(
                select(ChatMessage)
                .where(ChatMessage.session_id == chat.id)
                .order_by(ChatMessage.seq)
            )
        )
        return h, chat, msgs

    def _flush(self) -> None:
        try:
            self.s.flush()
        except StaleDataError as exc:
            raise ConflictError("already_handled", "someone else just updated this") from exc
