"""Booking-graph branches the conversation tests don't reach: several linked patients,
picking an appointment by day, asking for dates only once, empty lists, hook skip paths."""

from __future__ import annotations

from typing import Any

from netracare.agents.booking import templates as tpl
from netracare.agents.booking import tools as bt
from netracare.agents.booking.graph import _resolve_patient
from netracare.agents.booking.hitl import make_decision_hook
from netracare.agents.booking.state import AgentRuntime, PatientChoice
from netracare.db.models import ApprovalRequest, ChatSession
from tests.fixtures_api import did, local, pid, slot_ids_at
from tests.unit.agents.booking.test_graph import Env
from tests.unit.agents.booking.test_graph import env as env

FAMILY = [
    PatientChoice("p-self", "Asha Verma", False, True),
    PatientChoice("p-son", "Rohan Verma", True, False),
    PatientChoice("p-dad", "Mohan Rao", False, False),
]


def _rt(env: Env) -> AgentRuntime:
    rt = AgentRuntime(env.s, env.clock, env.settings, env.actor, env.thread)
    rt.patients = list(FAMILY)
    return rt


def _state(text: str, mention: str | None = None) -> dict[str, Any]:
    nlu = {
        "action": "book", "doctor_mention": None, "specialty_hint": None, "concern_phrase": None,
        "date_phrase": None, "time_phrase": None, "patient_mention": mention,
        "appointment_ref": None, "option_choice": None, "appt_type": None,
        "reply_kind": "answer", "confidence": 0.9,
    }  # fmt: skip
    return {"input": text, "nlu": nlu}


def test_resolve_patient_among_several(env: Env) -> None:
    rt = _rt(env)
    assert _resolve_patient(_state("for me please", "me"), rt)["patient_id"] == "p-self"  # type: ignore[arg-type,index]
    assert _resolve_patient(_state("for Mohan"), rt)["patient_id"] == "p-dad"  # type: ignore[arg-type,index]
    assert _resolve_patient(_state("my son needs a check"), rt)["patient_id"] == "p-son"  # type: ignore[arg-type,index]
    ask = _resolve_patient(_state("someone"), rt)  # type: ignore[arg-type]
    assert isinstance(ask, str) and "Asha Verma, Rohan Verma, Mohan Rao?" in ask
    rt.patients = []
    assert _resolve_patient(_state("me"), rt) == tpl.SIGN_IN  # type: ignore[arg-type]
    assert _resolve_patient({"patient_id": "x"}, rt) == {}  # type: ignore[arg-type]


def test_pick_appointment_by_day_among_several(env: Env) -> None:
    ctx = bt.ToolContext(env.s, env.clock, env.settings, env.actor, "seed")
    for i, (code, day, hh) in enumerate([("D1", 16, 10), ("D1", 19, 11)]):
        assert bt.book_appointment(ctx, patient_id=pid("P010"), doctor_id=did(code),
                                   slot_ids=slot_ids_at(env.s, code, local(day, hh)),
                                   proposal_id=f"s{i}").ok  # fmt: skip
    env.s.commit()
    t = env.say("cancel an appointment")
    assert t.kind == "question" and "1. " in t.text and "2. " in t.text
    rb = env.say("the one on 19 nov")
    assert rb.kind == "readback" and "Thursday 19 November 2026" in rb.text


def test_dates_are_asked_once_then_the_earliest_are_offered(env: Env) -> None:
    assert env.say("book with Dr Kavya Nair").text == tpl.ASK_DATES
    t = env.say("not sure yet")
    assert t.kind == "options" and "next two weeks" in t.text


def test_list_with_nothing_upcoming(env: Env) -> None:
    assert env.say("what are my appointments").text == tpl.LIST_EMPTY


def test_resume_while_collecting_asks_for_dates(env: Env) -> None:
    env.say("book with Dr Kavya Nair")
    env.agent.pause_for_safety(env.thread)
    t = env.say("continue booking")
    assert t.text == f"{tpl.RESUMED} {tpl.ASK_DATES}"


def test_decision_hook_skips_requests_without_a_chat(env: Env) -> None:
    hook = make_decision_hook(env.agent, env.settings, env.clock)
    req = ApprovalRequest()  # not raised from chat
    hook(env.s, req, "approved", {})  # no error, nothing to resume
    req.chat_session_id = "missing"
    hook(env.s, req, "approved", {})
    anon = ChatSession()
    env.s.add(anon)
    env.s.flush()
    req.chat_session_id = anon.id  # anonymous chat: no patient to resume for
    hook(env.s, req, "approved", {})
