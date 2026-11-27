"""The booking graph end to end over the seeded DB (frozen clock: Tue 10 Nov 2026 09:00 IST).

Most tests run without an LLM (keyword understanding); a few script `booking_nlu` replies.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session

from netracare.agents.booking import templates as tpl
from netracare.agents.booking.agent import AgentTurn, BookingAgent
from netracare.agents.booking.checkpoint import memory_checkpointer
from netracare.agents.booking.confirm import is_explicit_yes
from netracare.agents.booking.state import AgentRuntime
from netracare.db.models import (
    Appointment,
    ApprovalRequest,
    ChatHandoff,
    ChatSession,
    Holiday,
    User,
)
from netracare.db.session import make_session_factory
from netracare.domain.actor import CurrentUser
from netracare.domain.enums import AppointmentStatus, Role
from netracare.llm.client import LLMClient
from netracare.llm.pricing import Pricing
from netracare.llm.prompts import PromptRegistry
from netracare.llm.providers import FakeProvider
from tests.conftest import ROOT
from tests.fixtures_api import (
    NOW,
    did,
    guardian_email,
    local,
    make_settings,
    patient_email,
    slot_ids_at,
)
from tests.helpers import MutableClock
from tests.llm_helpers import resp


@dataclass
class Env:
    s: Session
    clock: MutableClock
    settings: Any
    agent: BookingAgent
    actor: CurrentUser
    thread: str = "thread-1"
    llm: LLMClient | None = None
    chat: ChatSession | None = None
    turns: list[AgentTurn] = field(default_factory=list)

    def say(self, text: str) -> AgentTurn:
        rt = AgentRuntime(
            self.s,
            self.clock,
            self.settings,
            self.actor,
            self.thread,
            self.llm,
            chat_session=self.chat,
        )
        t = self.agent.handle(rt, text)
        self.s.commit()
        self.turns.append(t)
        return t

    def count(self) -> int:
        return self.s.scalar(select(func.count()).select_from(Appointment)) or 0


def _actor(s: Session, email: str) -> CurrentUser:
    u = s.scalar(select(User).where(User.email == email))
    assert u is not None
    return CurrentUser(u.id, Role.PATIENT, u.email, u.email)


@pytest.fixture
def env(api_engine: Engine, api_db_url: str) -> Iterator[Env]:
    s = make_session_factory(api_engine)()
    e = Env(
        s,
        MutableClock(NOW),
        make_settings(api_db_url),
        BookingAgent(memory_checkpointer()),
        _actor(s, patient_email("P010")),
    )
    yield e
    s.rollback()
    s.close()


def _book_until_readback(env: Env, first: str = "book a check-up with Dr Kavya Nair") -> AgentTurn:
    env.say(first)
    t = env.say("next friday morning")
    assert t.kind == "options" and len(t.options) == 3
    return env.say("the second one")


def test_happy_path_books_after_explicit_yes(env: Env) -> None:
    rb = _book_until_readback(env)
    assert rb.kind == "readback" and rb.readback and "Friday 20 November 2026 at 10:00" in rb.text
    assert "Dr. Kavya Nair" in rb.text and rb.readback["needs_approval"] is False
    before = env.count()
    done = env.say("yes")
    assert done.kind == "confirmed" and done.wrote and done.step == "done"
    assert env.count() == before + 1
    appt = env.s.get(Appointment, done.appointment_id)
    assert appt is not None and appt.status is AppointmentStatus.CONFIRMED
    assert appt.start_utc == local(20, 10)


@pytest.mark.parametrize(
    "reply", ["yes but make it 11", "hmm maybe", "yes?", "not sure", "what about the doctor"]
)
def test_no_write_without_explicit_yes(env: Env, reply: str) -> None:
    _book_until_readback(env)
    before = env.count()
    t = env.say(reply)
    assert not t.wrote and env.count() == before
    assert "book_appointment" not in t.tool_calls


def test_change_at_readback_reproposes(env: Env) -> None:
    _book_until_readback(env)
    t = env.say("yes but make it the afternoon")
    assert t.kind == "options" and all(o["start_local"][11:13] >= "14" for o in t.options), [
        o["label"] for o in t.options
    ]


def test_stop_aborts_without_writing(env: Env) -> None:
    _book_until_readback(env)
    before = env.count()
    t = env.say("never mind")
    assert t.text == tpl.ABORTED and env.count() == before and t.step == "aborted"
    assert not env.agent.is_active(env.thread)


def test_missing_slots_are_asked_one_by_one(env: Env) -> None:
    t = env.say("I'd like an appointment")
    assert t.text == tpl.ASK_TARGET
    t = env.say("Dr Meera Iyer")
    assert t.text == tpl.ASK_DATES
    t = env.say("tomorrow")
    assert t.kind == "options" and all("Meera Iyer" in o["label"] for o in t.options)


def test_closed_day_and_past_and_out_of_hours(env: Env) -> None:
    env.say("book with Dr Kavya Nair")
    sunday = env.say("this sunday")
    assert "closed on Sundays" in sunday.text and sunday.kind == "question"
    late = env.say("monday at 8pm")
    assert late.text == tpl.OUT_OF_HOURS
    holiday_day = env.s.scalar(
        select(Holiday.day).where(Holiday.day > NOW.date()).order_by(Holiday.day)
    )
    if holiday_day is not None and holiday_day.weekday() != 6:
        t = env.say(f"{holiday_day.day} {holiday_day:%b}")
        assert "closed" in t.text


def test_list_appointments(env: Env) -> None:
    _book_until_readback(env)
    env.say("yes")
    t = env.say("what are my appointments")
    assert t.kind == "info" and "Dr. Kavya Nair" in t.text


def test_slot_taken_before_yes_reproposes_without_write(env: Env) -> None:
    rb = _book_until_readback(env)
    assert rb.kind == "readback"
    chosen = env.agent.state(env.thread)["chosen"]
    other = Env(
        env.s,
        env.clock,
        env.settings,
        BookingAgent(memory_checkpointer()),
        _actor(env.s, patient_email("P011")),
        thread="other",
    )
    from netracare.agents.booking import tools as bt

    ctx = bt.ToolContext(env.s, env.clock, env.settings, other.actor, "x")
    from tests.fixtures_api import pid

    assert bt.book_appointment(
        ctx,
        patient_id=pid("P011"),
        doctor_id=chosen["doctor_id"],
        slot_ids=chosen["slot_ids"],
        proposal_id="steal",
    ).ok
    env.s.commit()
    before = env.count()
    t = env.say("yes")
    assert t.kind == "options" and t.text.startswith(tpl.SLOT_GONE)
    assert env.count() == before and not t.wrote


def test_guardian_booking_for_child_needs_approval(env: Env) -> None:
    env.actor = _actor(env.s, guardian_email("P000"))
    t = env.say("book an eye check for my son")
    assert t.text == tpl.ASK_DATES  # only one linked patient (a minor) -> pediatrics by default
    t = env.say("next tuesday")
    assert t.kind == "options" and all("Arjun Menon" in o["label"] for o in t.options)
    rb = env.say("first")
    assert rb.readback and "guardian_booking_minor" in rb.readback["reasons"]
    assert "booking for a child" in rb.text
    done = env.say("yes")
    assert done.kind == "pending" and done.approval_request_id
    assert env.agent.pending(env.thread)[0]["approval_request_id"] == done.approval_request_id


def test_hitl_resume_after_front_desk_decision(env: Env) -> None:
    env.actor = _actor(env.s, guardian_email("P000"))
    env.say("book an eye check for my son")
    env.say("next tuesday")
    env.say("first")
    done = env.say("yes")
    rt = AgentRuntime(env.s, env.clock, env.settings, env.actor, env.thread)
    t = env.agent.on_approval_decided(
        rt, {"request_id": done.approval_request_id, "decision": "approved"}
    )
    assert t is not None and t.text.startswith("Good news")
    assert env.agent.pending(env.thread) == []
    assert (
        env.agent.on_approval_decided(rt, {"request_id": "unknown", "decision": "approved"}) is None
    )


@pytest.mark.parametrize("decision", ["rejected", "changed"])
def test_hitl_other_decisions(env: Env, decision: str) -> None:
    env.actor = _actor(env.s, guardian_email("P000"))
    env.say("book an eye check for my son")
    env.say("next tuesday")
    env.say("first")
    done = env.say("yes")
    rt = AgentRuntime(env.s, env.clock, env.settings, env.actor, env.thread)
    ev = {
        "request_id": done.approval_request_id,
        "decision": decision,
        "note": "doctor away",
        "start_local": "2026-11-18T10:00:00+05:30",
        "doctor_name": "Dr. Arjun Menon",
    }
    t = env.agent.on_approval_decided(rt, ev)
    assert t is not None
    if decision == "rejected":
        assert "could not approve" in t.text and "doctor away" in t.text
    else:
        assert "Wednesday 18 November 2026 at 10:00" in t.text


def test_reschedule_within_24h_is_pending(env: Env) -> None:
    from netracare.agents.booking import tools as bt
    from tests.fixtures_api import pid

    ctx = bt.ToolContext(env.s, env.clock, env.settings, env.actor, "seed")
    booked = bt.book_appointment(
        ctx,
        patient_id=pid("P010"),
        doctor_id=did("D5"),
        slot_ids=slot_ids_at(env.s, "D5", local(10, 16)),
        proposal_id="s",
    )
    env.s.commit()
    t = env.say("please move my appointment to friday")
    assert t.kind == "options"
    rb = env.say("first")
    assert rb.readback and "within_24h_change" in rb.readback["reasons"]
    done = env.say("yes")
    assert done.kind == "pending"
    old = env.s.get(Appointment, booked.data["appointment_id"])
    assert old is not None and old.status is AppointmentStatus.CONFIRMED  # untouched until approved


def test_cancel_flow_and_no_keeps_appointment(env: Env) -> None:
    from netracare.agents.booking import tools as bt
    from tests.fixtures_api import pid

    ctx = bt.ToolContext(env.s, env.clock, env.settings, env.actor, "seed")
    booked = bt.book_appointment(
        ctx,
        patient_id=pid("P010"),
        doctor_id=did("D1"),
        slot_ids=slot_ids_at(env.s, "D1", local(16, 10)),
        proposal_id="c",
    )
    env.s.commit()
    rb = env.say("cancel my appointment")
    assert rb.kind == "readback" and "cancel" in rb.text.lower()
    t = env.say("no")
    assert t.text == tpl.ABORTED
    assert (
        env.s.get(Appointment, booked.data["appointment_id"]).status is AppointmentStatus.CONFIRMED
    )  # type: ignore[union-attr]
    env.say("cancel my appointment")
    done = env.say("yes")
    assert done.kind == "confirmed" and "cancelled" in done.text


def test_two_tool_failures_escalate_to_a_human(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    env.chat = ChatSession(user_id=env.actor.id)
    env.s.add(env.chat)
    env.s.flush()
    _book_until_readback(env)
    from netracare.services.booking_service import BookingService

    def boom(self, actor, cmd):  # type: ignore[no-untyped-def]
        raise RuntimeError("db down")

    monkeypatch.setattr(BookingService, "book", boom)
    before = env.count()
    t = env.say("yes")
    assert t.text == tpl.ESCALATED and env.count() == before
    assert t.tool_calls.count("book_appointment") == 2 and "escalate_to_human" in t.tool_calls
    assert env.s.scalar(select(func.count()).select_from(ChatHandoff)) == 1


def test_emergency_booking_reason_pauses(env: Env) -> None:
    env.say("book with Dr Kavya Nair, sudden curtain over my vision")
    env.say("next friday morning")
    env.say("first")
    t = env.say("yes")
    assert t.step == "paused_safety" and "emergency" in t.text.lower()
    assert t.safety.get("level") == "emergency"
    assert env.agent.is_paused(env.thread) and env.agent.wants_resume(
        env.thread, "continue booking"
    )


def test_pause_and_resume_for_safety(env: Env) -> None:
    rb = _book_until_readback(env)
    env.agent.pause_for_safety(env.thread)
    assert env.agent.is_paused(env.thread) and not env.agent.is_active(env.thread)
    t = env.say("I'm ok now, continue booking")
    assert t.text.startswith(tpl.RESUMED) and t.step == "readback"
    assert env.say("yes").kind == "confirmed"
    assert rb.kind == "readback"


def test_stale_flow_expires(env: Env) -> None:
    env.say("book with Dr Kavya Nair")
    env.clock.advance(timedelta(hours=25))
    t = env.say("next monday")
    assert t.text.startswith(tpl.EXPIRED)


def test_old_proposal_is_rechecked_before_writing(env: Env) -> None:
    _book_until_readback(env)
    env.clock.advance(timedelta(minutes=45))
    before = env.count()
    t = env.say("yes")
    assert t.text.startswith(tpl.SLOT_RECHECK) and env.count() == before


def test_option_buttons_and_llm_understanding(env: Env, make_llm: Any) -> None:
    script = [
        {
            "action": "book",
            "doctor_mention": None,
            "specialty_hint": "RETINA",
            "concern_phrase": "routine diabetic check",
            "date_phrase": "next Tuesday",
            "time_phrase": "morning",
            "patient_mention": None,
            "appointment_ref": None,
            "option_choice": None,
            "appt_type": None,
            "reply_kind": "answer",
            "confidence": 0.9,
        },
    ]
    env.llm = make_llm(FakeProvider([resp(script[0])]))
    t = env.say("I'm diabetic, book me a retina check next Tuesday morning")
    assert t.kind == "options" and all("Vikram Shetty" in o["label"] for o in t.options)
    assert t.llm_call_ids
    rb = env.say(f"option:{t.options[2]['id']}")  # button: no LLM call needed
    assert rb.kind == "readback" and rb.llm_call_ids == []


def test_every_write_follows_a_readback_and_an_explicit_yes(env: Env) -> None:
    """Property over a mixed conversation: writes only on explicit-yes turns after a read-back."""
    for text in [
        "book a check-up with Dr Kavya Nair",
        "next friday morning",
        "the second one",
        "yes but make it 11",
        "first",
        "hmm",
        "yes",
        "book with Dr Meera Iyer",
        "tomorrow",
        "1",
        "ok",
        "cancel my appointment",
        "nope",
        "list my appointments",
    ]:
        env.say(text)
    writes = env.agent.state(env.thread)["writes"]
    assert writes, "the conversation should have written something"
    for w in writes:
        assert 0 <= w["readback_turn"] < w["turn"], w
        assert is_explicit_yes(w["yes_text"]), w


def test_anonymous_or_unlinked_user_is_asked_to_sign_in(env: Env) -> None:
    env.actor = CurrentUser("no-such-user", Role.PATIENT, "x@y", "x")
    assert env.say("book with Dr Kavya Nair").text == tpl.SIGN_IN


def test_approval_request_is_created(env: Env) -> None:
    env.actor = _actor(env.s, guardian_email("P000"))
    env.say("book an eye check for my son")
    env.say("next tuesday")
    env.say("first")
    done = env.say("yes")
    req = env.s.get(ApprovalRequest, done.approval_request_id)
    assert req is not None and req.status.value == "pending"


def test_checkpointer_shared_llm_registry_loads() -> None:
    assert (
        PromptRegistry(ROOT / "prompts").load().get("booking_nlu").spec.output_schema
        == "BookingTurn"
    )
    assert Pricing.load(ROOT / "config" / "model_pricing.yaml")
