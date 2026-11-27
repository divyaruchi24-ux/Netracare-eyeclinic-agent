"""Human in the loop: the approval-decision hook and the overdue notice.

* `make_decision_hook(agent, settings, clock)` is registered in `approval_service.DECISION_HOOKS`
  by the app. When the front desk approves / edits / rejects a request raised from chat, the
  agent's thread is resumed with the decision and the outcome is posted into the conversation
  (and the usual notification goes to the outbox, as for any approval).
* `notify_overdue` (policy section 12): a request pending longer than APPROVAL_OVERDUE_HOURS
  gets the "we haven't confirmed this yet" text once - in the outbox and, for chat requests,
  in the conversation. Runs from `python -m netracare.agents.booking.jobs overdue` and when
  the patient next writes in the chat.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from netracare.agents.booking import templates as tpl
from netracare.agents.booking.agent import BookingAgent
from netracare.agents.booking.state import AgentRuntime
from netracare.config import Settings
from netracare.db.models import ApprovalRequest, ChatMessage, ChatSession, User
from netracare.domain.actor import CurrentUser
from netracare.domain.clock import Clock
from netracare.domain.enums import ApprovalStatus, ChatRole
from netracare.log import get_logger
from netracare.services.approval_service import DecisionHook
from netracare.services.notification_service import NotificationService

log = get_logger(__name__)


def append_assistant(
    session: Session,
    chat: ChatSession,
    text: str,
    *,
    clock: Clock,
    intent: str = "booking",
    agent: dict[str, Any] | None = None,
) -> ChatMessage:
    seq = session.scalar(select(func.max(ChatMessage.seq)).where(ChatMessage.session_id == chat.id))
    msg = ChatMessage(
        session_id=chat.id,
        seq=(seq or 0) + 1,
        role=ChatRole.ASSISTANT,
        text=text,
        intent=intent,
        mode="agent",
        agent=agent,
        created_at=clock.now_utc(),
    )
    session.add(msg)
    session.flush()
    return msg


def _actor_for(session: Session, chat: ChatSession) -> CurrentUser | None:
    if chat.user_id is None:
        return None
    user = session.get(User, chat.user_id)
    if user is None:
        return None
    return CurrentUser(user.id, user.role, user.email, user.email)


def make_decision_hook(agent: BookingAgent, settings: Settings, clock: Clock) -> DecisionHook:
    def hook(session: Session, req: ApprovalRequest, decision: str, extra: dict[str, Any]) -> None:
        if not req.chat_session_id:
            return
        chat = session.get(ChatSession, req.chat_session_id)
        actor = _actor_for(session, chat) if chat else None
        if chat is None or actor is None:
            return
        rt = AgentRuntime(session, clock, settings, actor, chat.id)
        turn = agent.on_approval_decided(rt, {"request_id": req.id, "decision": decision, **extra})
        if turn is not None:
            append_assistant(
                session,
                chat,
                turn.text,
                clock=clock,
                agent={"kind": "decided", "decision": decision},
            )
            log.info("agent.resumed_after_approval", decision=decision, request_id=req.id)

    return hook


def notify_overdue(
    session: Session, req: ApprovalRequest, settings: Settings, clock: Clock
) -> bool:
    """Send the section 12 overdue notice for one request, once. True if it was sent now."""
    if req.status is not ApprovalStatus.PENDING or req.notified_overdue_at is not None:
        return False
    if clock.now_utc() - req.created_at <= timedelta(hours=settings.approval_overdue_hours):
        return False
    from netracare.db.models import Appointment

    appt = session.get(Appointment, req.target_appointment_id or req.appointment_id)
    if appt is not None:
        NotificationService(session, settings.tz).for_appointment(appt, "approval_overdue")
    if req.chat_session_id:
        chat = session.get(ChatSession, req.chat_session_id)
        if chat is not None:
            append_assistant(session, chat, tpl.OVERDUE, clock=clock, agent={"kind": "overdue"})
    req.notified_overdue_at = clock.now_utc()
    session.flush()
    return True


def overdue_for_chat(session: Session, chat_id: str, settings: Settings, clock: Clock) -> int:
    reqs = session.scalars(
        select(ApprovalRequest).where(
            ApprovalRequest.chat_session_id == chat_id,
            ApprovalRequest.status == ApprovalStatus.PENDING,
            ApprovalRequest.notified_overdue_at.is_(None),
        )
    )
    return sum(notify_overdue(session, r, settings, clock) for r in reqs)


def run_overdue(session: Session, settings: Settings, clock: Clock) -> int:
    reqs = session.scalars(
        select(ApprovalRequest).where(
            ApprovalRequest.status == ApprovalStatus.PENDING,
            ApprovalRequest.notified_overdue_at.is_(None),
        )
    )
    return sum(notify_overdue(session, r, settings, clock) for r in reqs)
