import json
from collections.abc import Callable

import pytest

from netracare.agents.router import Router, keyword_fallback
from netracare.agents.router_rules import layer1
from netracare.domain.enums import ChatIntent
from netracare.llm.client import LLMClient
from netracare.llm.providers import DisabledProvider, FakeProvider
from netracare.llm.types import BudgetExceeded, LLMUnavailable
from netracare.rag.doctor_names import DoctorName
from tests.conftest import ROOT
from tests.llm_helpers import resp

MakeLLM = Callable[..., LLMClient]
DOCTORS = [DoctorName("D2", "Dr. Vikram Shetty")]


def router_json(intent: str = "faq_rag", **kw: object) -> dict[str, object]:
    base: dict[str, object] = {
        "intent": intent,
        "confidence": 0.9,
        "standalone_query": "What are the clinic opening hours?",
        "specialty_hint": None,
        "doctor_mention": None,
        "concern_phrase": None,
    }
    base.update(kw)
    return base


def test_llm_route_with_followup_rewrite(make_llm: MakeLLM) -> None:
    fake = FakeProvider(
        [resp(router_json("pricing", standalone_query="How much does LASIK cost?"))]
    )
    d = Router(make_llm(fake)).route("and how much is it?", "user: tell me about LASIK")
    assert d.intent is ChatIntent.PRICING and d.source == "llm"
    assert d.standalone_query == "How much does LASIK cost?"
    assert d.llm_call_ids and d.cost_usd > 0
    sent = fake.sent_text()
    assert "<message>\nand how much is it?" in sent and "tell me about LASIK" in sent


def test_concern_phrase_is_sanitised(make_llm: MakeLLM) -> None:
    fake = FakeProvider([resp(router_json("booking", concern_phrase="probable glaucoma damage"))])
    assert Router(make_llm(fake)).route("book me").concern_phrase is None
    fake = FakeProvider([resp(router_json("booking", concern_phrase="new floaters"))])
    assert Router(make_llm(fake)).route("book me").concern_phrase == "new floaters"


def test_empty_standalone_falls_back_to_message(make_llm: MakeLLM) -> None:
    fake = FakeProvider([resp(router_json(standalone_query="  "))])
    assert Router(make_llm(fake)).route("parking?").standalone_query == "parking?"


@pytest.mark.parametrize("err", [LLMUnavailable("down"), BudgetExceeded("daily")])
def test_llm_errors_use_keyword_fallback(make_llm: MakeLLM, err: Exception) -> None:
    d = Router(make_llm(FakeProvider([err])), DOCTORS).route("how much is lasik")
    assert d.intent is ChatIntent.PRICING and d.source == "keyword_fallback"
    assert d.error in {"llm_unavailable", "budget_exceeded"}


def test_disabled_llm_uses_keyword_fallback(make_llm: MakeLLM) -> None:
    d = Router(make_llm(DisabledProvider())).route("book an appointment")
    assert d.intent is ChatIntent.BOOKING and d.error == "llm_disabled"
    assert Router(None).route("parking?").intent is ChatIntent.FAQ_RAG


@pytest.mark.parametrize(
    ("text", "intent"),
    [
        ("I want to book for Tuesday", ChatIntent.BOOKING),
        ("how much is lasik", ChatIntent.PRICING),
        ("Dr Shetti timings", ChatIntent.DOCTOR_LOOKUP),
        ("who is the cornea specialist", ChatIntent.DOCTOR_LOOKUP),
        ("is there parking", ChatIntent.FAQ_RAG),
    ],
)
def test_keyword_fallback(text: str, intent: ChatIntent) -> None:
    assert keyword_fallback(text, DOCTORS) is intent


def test_llm_never_sees_messages_layer1_decided() -> None:
    """Property over every red-flag eval case: the deterministic layer decides emergencies and
    urgent cases itself, so the LLM router cannot downgrade them."""
    path = ROOT / "evals/datasets/safety_redflags_v0.jsonl"
    for line in path.read_text("utf-8").splitlines():
        case = json.loads(line)
        if case["expected"]["level"] in ("emergency", "urgent"):
            assert layer1(case["input"]["message"]).intent is ChatIntent.EMERGENCY, case["id"]
