"""HITL: front-desk approve / edit / reject resumes the chat thread; overdue notices."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import timedelta

import pytest
from sqlalchemy import func, select

from netracare.agents.booking import templates as tpl
from netracare.agents.booking.hitl import make_decision_hook, overdue_for_chat, run_overdue
from netracare.db.models import (
    Appointment,
    AppointmentSlot,
    ApprovalRequest,
    ChatMessage,
    ChatSession,
    NotificationOutbox,
)
from netracare.domain.actor import CurrentUser
from netracare.domain.enums import AppointmentStatus, Role
from netracare.errors import ConflictError, ValidationAppError
from netracare.services import approval_service
from netracare.services.approval_service import ApprovalService
from tests.fixtures_api import guardian_email, local, slot_ids_at
from tests.unit.agents.booking.test_graph import Env, _actor
from tests.unit.agents.booking.test_graph import env as env

STAFF: CurrentUser


@pytest.fixture
def pending(env: Env) -> Iterator[tuple[Env, ApprovalRequest]]:
    """A guardian's chat booking for a child (needs approval), linked to a chat session."""
    env.actor = _actor(env.s, guardian_email("P000"))
    env.chat = ChatSession(user_id=env.actor.id)
    env.s.add(env.chat)
    env.s.flush()
    env.thread = env.chat.id
    env.say("book an eye check for my son")
    env.say("next tuesday")
    env.say("first")
    done = env.say("yes")
    req = env.s.get(ApprovalRequest, done.approval_request_id)
    assert req is not None and req.source == "chat" and req.chat_session_id == env.chat.id
    global STAFF
    from netracare.db.models import User
    from tests.fixtures_api import FRONTDESK

    fd = env.s.scalar(select(User).where(User.email == FRONTDESK))
    assert fd is not None
    STAFF = CurrentUser(fd.id, Role.FRONT_DESK, fd.email, "Front desk")
    hook = make_decision_hook(env.agent, env.settings, env.clock)
    approval_service.DECISION_HOOKS.append(hook)
    yield env, req
    approval_service.DECISION_HOOKS.remove(hook)


def _chat_texts(env: Env) -> list[str]:
    assert env.chat is not None
    return list(
        env.s.scalars(
            select(ChatMessage.text)
            .where(ChatMessage.session_id == env.chat.id)
            .order_by(ChatMessage.seq)
        )
    )


def test_approve_resumes_the_thread(pending: tuple[Env, ApprovalRequest]) -> None:
    env, req = pending
    ApprovalService(env.s, env.clock, env.settings).approve(STAFF, req.id, "ok")
    assert any(t.startswith("Good news") for t in _chat_texts(env))
    assert env.agent.pending(env.thread) == []


def test_reject_resumes_with_the_note(pending: tuple[Env, ApprovalRequest]) -> None:
    env, req = pending
    ApprovalService(env.s, env.clock, env.settings).reject(STAFF, req.id, "doctor is away")
    texts = _chat_texts(env)
    assert any("could not approve" in t and "doctor is away" in t for t in texts)


def test_approve_with_changes_swaps_the_slot(pending: tuple[Env, ApprovalRequest]) -> None:
    env, req = pending
    appt = env.s.get(Appointment, req.appointment_id)
    assert appt is not None and appt.status is AppointmentStatus.PENDING_APPROVAL
    old_start = appt.start_utc
    new_slots = slot_ids_at(env.s, "D4", local(19, 15))
    ApprovalService(env.s, env.clock, env.settings).approve_with_changes(
        STAFF, req.id, new_slots, "moved to Thursday"
    )
    env.s.refresh(appt)
    assert appt.status is AppointmentStatus.CONFIRMED and appt.start_utc == local(19, 15)
    assert appt.start_utc != old_start
    held = set(
        env.s.scalars(
            select(AppointmentSlot.slot_id).where(
                AppointmentSlot.appointment_id == appt.id, AppointmentSlot.active
            )
        )
    )
    assert held == set(new_slots)
    assert any(
        "different time" in t and "Thursday 19 November 2026 at 15:00" in t
        for t in _chat_texts(env)
    )
    templates = list(env.s.scalars(select(NotificationOutbox.template)))
    assert "approval_changed" in templates


def test_approve_with_changes_conflict_and_validation(pending: tuple[Env, ApprovalRequest]) -> None:
    env, req = pending
    svc = ApprovalService(env.s, env.clock, env.settings)
    with pytest.raises(ValidationAppError):
        svc.approve_with_changes(STAFF, req.id, [])
    # the new time is taken by someone else meanwhile
    from netracare.agents.booking import tools as bt
    from tests.fixtures_api import did, patient_email, pid

    other = _actor(env.s, patient_email("P011"))
    taken = slot_ids_at(env.s, "D4", local(19, 16))
    ctx = bt.ToolContext(env.s, env.clock, env.settings, other, "o")
    assert bt.book_appointment(
        ctx, patient_id=pid("P011"), doctor_id=did("D4"), slot_ids=taken, proposal_id="x"
    ).ok
    with pytest.raises(ConflictError):
        svc.approve_with_changes(STAFF, req.id, taken)


def test_hook_failure_never_blocks_the_decision(pending: tuple[Env, ApprovalRequest]) -> None:
    env, req = pending

    def broken(*_a: object) -> None:
        raise RuntimeError("agent down")

    approval_service.DECISION_HOOKS.insert(0, broken)
    try:
        view = ApprovalService(env.s, env.clock, env.settings).approve(STAFF, req.id)
    finally:
        approval_service.DECISION_HOOKS.remove(broken)
    assert view.request.status.value == "approved"


def test_overdue_notice_is_sent_once(pending: tuple[Env, ApprovalRequest]) -> None:
    env, req = pending
    assert env.chat is not None
    assert overdue_for_chat(env.s, env.chat.id, env.settings, env.clock) == 0  # not yet overdue
    env.clock.advance(timedelta(hours=5))
    assert overdue_for_chat(env.s, env.chat.id, env.settings, env.clock) == 1
    assert run_overdue(env.s, env.settings, env.clock) == 0  # already notified
    assert tpl.OVERDUE in _chat_texts(env)
    n = env.s.scalar(
        select(func.count())
        .select_from(NotificationOutbox)
        .where(NotificationOutbox.template == "approval_overdue")
    )
    assert n == 1 and req.notified_overdue_at is not None


def test_overdue_job_cli(
    pending: tuple[Env, ApprovalRequest],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    env, _ = pending
    env.s.commit()
    import netracare.config as config
    from netracare.agents.booking import jobs

    url = str(env.s.get_bind().url)
    monkeypatch.setattr(
        config, "get_settings", lambda: env.settings.model_copy(update={"database_url": url})
    )
    assert jobs.main(["overdue"]) == 0
    assert "overdue approval notice" in capsys.readouterr().out
