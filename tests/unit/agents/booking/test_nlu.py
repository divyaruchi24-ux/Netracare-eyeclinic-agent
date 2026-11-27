from collections.abc import Callable
from typing import Any

import pytest

from netracare.agents.booking.nlu import choose_option, keyword_fallback, understand
from netracare.llm.client import LLMClient
from netracare.llm.providers import DisabledProvider, FakeProvider
from netracare.llm.types import LLMUnavailable
from tests.llm_helpers import resp

MakeLLM = Callable[..., LLMClient]
OPTIONS = [
    {"id": "a", "start_local": "2026-11-17T09:15:00+05:30"},
    {"id": "b", "start_local": "2026-11-17T10:30:00+05:30"},
    {"id": "c", "start_local": "2026-11-17T16:00:00+05:30"},
]


def turn_json(**kw: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "action": "book",
        "doctor_mention": None,
        "specialty_hint": None,
        "concern_phrase": None,
        "date_phrase": None,
        "time_phrase": None,
        "patient_mention": None,
        "appointment_ref": None,
        "option_choice": None,
        "appt_type": None,
        "reply_kind": "answer",
        "confidence": 0.9,
    }
    base.update(kw)
    return base


def test_understand_with_llm(make_llm: MakeLLM) -> None:
    fake = FakeProvider([resp(turn_json(date_phrase="next Tuesday", time_phrase="morning"))])
    turn, ids, cost = understand(make_llm(fake), "book next tuesday morning", "")
    assert turn.date_phrase == "next Tuesday" and turn.time_phrase == "morning"
    assert ids and cost > 0
    assert "(no booking in progress)" in fake.sent_text()


def test_understand_falls_back(make_llm: MakeLLM) -> None:
    turn, ids, _ = understand(make_llm(FakeProvider([LLMUnavailable("x")])), "cancel it", "")
    assert turn.action == "cancel" and ids == []
    turn, _, _ = understand(make_llm(DisabledProvider()), "yes", "", current_action="book")
    assert turn.reply_kind == "yes" and turn.action == "book"
    assert understand(None, "list my appointments", "")[0].action == "list"


@pytest.mark.parametrize(
    ("text", "action", "kind"),
    [
        ("I want to book with Dr Rao next tuesday", "book", "answer"),
        ("please move my appointment to friday", "reschedule", "answer"),
        ("cancel my appointment tomorrow", "cancel", "answer"),
        ("what are my appointments", "list", "answer"),
        ("never mind", "none", "stop"),
        ("no", "none", "no"),
    ],
)
def test_keyword_fallback(text: str, action: str, kind: str) -> None:
    t = keyword_fallback(text)
    assert (t.action, t.reply_kind) == (action, kind)


def test_keyword_fallback_marks_changes_in_a_flow() -> None:
    t = keyword_fallback("make it thursday afternoon", "book")
    assert t.reply_kind == "change" and t.date_phrase and t.time_phrase and t.action == "book"
    assert keyword_fallback("Dr Shetty please").doctor_mention == "Dr Shetty please"


@pytest.mark.parametrize(
    ("text", "index"),
    [
        ("option:b", 1),
        ("2", 1),
        ("#3", 2),
        ("option 1", 0),
        ("the second one", 1),
        ("first", 0),
        ("the last one", 2),
        ("the 10:30 one", 1),
        ("4pm please", 2),
        ("9", None),
        ("option:zzz", None),
        ("whatever", None),
        ("around 10", 1),
        ("in the morning", None),
    ],
)
def test_choose_option(text: str, index: int | None) -> None:
    assert choose_option(text, None, OPTIONS) == index


def test_choose_option_uses_llm_choice_and_bounds() -> None:
    from netracare.llm.schemas import BookingTurn

    turn = BookingTurn.model_validate(turn_json(option_choice=3))
    assert choose_option("that one", turn, OPTIONS) == 2
    assert (
        choose_option("x", BookingTurn.model_validate(turn_json(option_choice=7)), OPTIONS) is None
    )
    assert choose_option("1", None, []) is None
