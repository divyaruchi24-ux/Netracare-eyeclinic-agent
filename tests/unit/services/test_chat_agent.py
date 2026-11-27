"""ChatService + booking agent: routing into the agent, safety pauses, FAQ mid-flow, overdue."""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select

from netracare.agents.booking import templates as tpl
from netracare.agents.booking.agent import BookingAgent
from netracare.agents.booking.checkpoint import memory_checkpointer
from netracare.db.models import ApprovalRequest, ChatMessage
from netracare.domain.enums import ChatIntent
from netracare.guardrails import scripts
from netracare.services.chat_service import ChatService
from tests.fixtures_api import guardian_email
from tests.llm_helpers import answer_json, route_json, triage_json
from tests.unit.services.test_chat_service import Env
from tests.unit.services.test_chat_service import env as env


class _Committing(ChatService):
    """Commits after each turn, like the API's request scope does."""

    def post_message(self, *a, **kw):  # type: ignore[no-untyped-def]
        turn = super().post_message(*a, **kw)
        self.s.commit()
        return turn


def service(env: Env, agent: BookingAgent) -> ChatService:
    base = env.service()
    svc = _Committing(base.s, base.clock, base.settings, base.llm, base.index, agent)
    return svc


@pytest.fixture
def agent() -> BookingAgent:
    return BookingAgent(memory_checkpointer())


def test_signed_in_booking_goes_through_the_agent(env: Env, agent: BookingAgent) -> None:
    env.script.router.append(route_json("booking", "book with Dr Kavya Nair next friday"))
    svc = service(env, agent)
    me = env.patient("P010")
    chat, _ = svc.create_session(me)
    t = svc.post_message(
        chat.id, "book a check-up with Dr Kavya Nair next friday morning", actor=me
    )
    assert t.intent is ChatIntent.BOOKING and t.mode == "agent" and len(t.options) == 3
    assert t.agent_step == "propose"
    rb = svc.post_message(chat.id, "2", actor=me)  # no router call while the agent is active
    assert rb.readback and rb.agent_step == "readback"
    assert env.script.seen.count("router") == 1
    done = svc.post_message(chat.id, "yes", actor=me)
    assert done.agent_step == "done" and "confirmed" in done.reply
    stored = env.s.scalar(
        select(ChatMessage)
        .where(ChatMessage.session_id == chat.id)
        .order_by(ChatMessage.seq.desc())
    )
    assert stored is not None and stored.agent and stored.agent["step"] == "done"
    first_reply = env.s.scalar(
        select(ChatMessage).where(ChatMessage.session_id == chat.id, ChatMessage.seq == 2)
    )
    assert first_reply is not None and first_reply.agent and len(first_reply.agent["options"]) == 3


def test_anonymous_booking_keeps_the_page_link(env: Env, agent: BookingAgent) -> None:
    env.script.router.append(route_json("booking", "book an appointment"))
    svc = service(env, agent)
    chat, token = svc.create_session(None)
    t = svc.post_message(chat.id, "I want to book an appointment", actor=None, anon_token=token)
    assert t.mode == "fixed" and t.suggested_action and not t.options


def test_emergency_mid_flow_pauses_then_resumes(env: Env, agent: BookingAgent) -> None:
    env.script.router.append(route_json("booking", "book"))
    # message 1 (a booking with "check-up") gets a routine triage; message 2 the emergency
    env.script.triage += [triage_json("routine"), triage_json("emergency", ["E4"], confidence=0.95)]
    svc = service(env, agent)
    me = env.patient("P010")
    chat, _ = svc.create_session(me)
    svc.post_message(chat.id, "book a check-up with Dr Kavya Nair next friday morning", actor=me)
    t = svc.post_message(chat.id, "wait, bleach just splashed in my eye", actor=me)
    assert t.intent is ChatIntent.EMERGENCY and t.reply == scripts.emergency_message(["E4"])
    assert agent.is_paused(chat.id)
    back = svc.post_message(chat.id, "I'm safe now, continue booking", actor=me)
    assert back.reply.startswith(tpl.RESUMED) and back.options


def test_faq_question_mid_flow_is_answered_and_the_flow_kept(env: Env, agent: BookingAgent) -> None:
    env.script.router += [route_json("booking", "book"), route_json("faq_rag", "Is there parking?")]
    env.script.answer.append(answer_json("Basement parking is available [1].", [1]))
    svc = service(env, agent)
    me = env.patient("P010")
    chat, _ = svc.create_session(me)
    svc.post_message(chat.id, "book a check-up with Dr Kavya Nair next friday morning", actor=me)
    t = svc.post_message(chat.id, "is there parking?", actor=me)
    assert t.intent is ChatIntent.FAQ_RAG and t.reply.endswith(tpl.CONTINUE_LATER)
    assert agent.is_active(chat.id)
    assert svc.post_message(chat.id, "first", actor=me).readback


def test_urgent_symptom_with_booking_request_gets_script_then_options(
    env: Env, agent: BookingAgent
) -> None:
    svc = service(env, agent)
    me = env.patient("P010")
    chat, _ = svc.create_session(me)
    t = svc.post_message(chat.id, "a few new floaters, no flashes. book me next tuesday", actor=me)
    assert t.reply.startswith(scripts.URGENT_SCRIPT) and t.options
    assert all("Vikram Shetty" in o["label"] for o in t.options)
    rb = svc.post_message(chat.id, "first", actor=me)
    assert rb.readback and "post_red_flag" in rb.readback["reasons"]


def test_overdue_notice_after_a_pending_chat_request(env: Env, agent: BookingAgent) -> None:
    from netracare.db.models import User
    from netracare.domain.actor import CurrentUser
    from netracare.domain.enums import Role

    env.script.router.append(route_json("booking", "book for my son"))
    svc = service(env, agent)
    u = env.s.scalar(select(User).where(User.email == guardian_email("P000")))
    assert u is not None
    guardian = CurrentUser(u.id, Role.PATIENT, u.email, u.email)
    chat, _ = svc.create_session(guardian)
    svc.post_message(chat.id, "book an eye check for my son", actor=guardian)
    svc.post_message(chat.id, "next tuesday", actor=guardian)
    svc.post_message(chat.id, "first", actor=guardian)
    done = svc.post_message(chat.id, "yes", actor=guardian)
    assert "front desk for approval" in done.reply
    req = env.s.scalar(select(ApprovalRequest).where(ApprovalRequest.chat_session_id == chat.id))
    assert req is not None and req.source == "chat"
    env.clock.advance(timedelta(hours=5))
    svc.post_message(chat.id, "thanks", actor=guardian)
    texts = [m.text for m in svc.messages(chat)]
    assert texts.count(tpl.OVERDUE) == 1
    svc.post_message(chat.id, "thanks again", actor=guardian)
    assert [m.text for m in svc.messages(chat)].count(tpl.OVERDUE) == 1  # only once
