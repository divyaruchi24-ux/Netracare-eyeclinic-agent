"""Phase 3 UI: Ask NetraCare chat, front-desk chat handoffs, admin knowledge base."""

from __future__ import annotations

from collections.abc import Callable

from fastapi.testclient import TestClient
from streamlit.testing.v1 import AppTest

from netracare.guardrails import scripts
from netracare.llm.providers import FakeProvider
from netracare.ui.api_client import ApiClient
from netracare.ui.components import PREFILL_KEY, md_lines
from tests.fixtures_api import ADMIN, FRONTDESK, patient_email
from tests.llm_helpers import ChatScript, answer_json, route_json

UI = Callable[[str | None], AppTest]


def _open_chat(at: AppTest) -> AppTest:
    at.switch_page("app_pages/chat.py").run()
    assert not at.exception, at.exception
    return at


def _send(at: AppTest, text: str) -> AppTest:
    at.chat_input(key="chat_input").set_value(text).run()
    assert not at.exception, at.exception
    return at


def test_anonymous_visitor_can_chat_with_sources_and_footer(
    ui: UI, use_llm: Callable[..., FakeProvider]
) -> None:
    script = ChatScript()
    script.router.append(route_json("faq_rag", "Is there parking at the clinic?"))
    script.answer.append(answer_json("Basement parking is available [1].", [1]))
    use_llm(script)
    at = _send(_open_chat(ui(None)), "is there parking?")
    texts = [m.value for m in at.markdown]
    assert "is there parking?" in texts
    assert "Basement parking is available [1]." in texts
    assert any(e.label.startswith("Sources (1)") for e in at.expander)
    assert any(c.value == scripts.CHAT_FOOTER for c in at.caption)
    assert any(c.value == scripts.DISCLAIMERS["kb_answer"] for c in at.caption)


def test_emergency_reply_is_a_red_box(ui: UI) -> None:
    at = _send(_open_chat(ui(patient_email("P010"))), "bleach got in my eye")
    assert at.error[0].value == md_lines(scripts.emergency_message(["E4"]))


def test_booking_suggestion_prefills_the_booking_page(
    ui: UI, use_llm: Callable[..., FakeProvider]
) -> None:
    """Signed-in booking requests go to the booking agent (Phase 4); the pre-filled booking
    page button remains on medical-question refusals ("our Cornea specialist ...")."""
    use_llm(ChatScript())
    at = _send(_open_chat(ui(patient_email("P010"))), "Which eye drops should I use for dry eyes?")
    button = next(b for b in at.button if b.label == "Book with Dr. Ananya Rao")
    button.click().run()
    assert not at.exception, at.exception
    assert at.session_state["patient_book_spec"] == "CORNEA"
    assert at.session_state["patient_book_doctor"]["full_name"] == "Dr. Ananya Rao"
    assert PREFILL_KEY not in at.session_state


def test_anonymous_booking_suggestion_asks_to_sign_in(
    ui: UI, use_llm: Callable[..., FakeProvider]
) -> None:
    script = ChatScript()
    script.router.append(route_json("booking", "book"))
    use_llm(script)
    at = _send(_open_chat(ui(None)), "I want to book an appointment")
    assert any("please sign in first" in c.value for c in at.caption)


def test_talk_to_a_person_and_front_desk_handoff_page(
    ui: UI, api_for: Callable[[str | None], ApiClient], client: TestClient
) -> None:
    at = _open_chat(ui(patient_email("P010")))
    next(b for b in at.button if b.label == "Talk to a person").click().run()
    assert not at.exception, at.exception
    assert any(md_lines(scripts.HANDOFF_CREATED) == m.value for m in at.markdown)

    desk = ui(FRONTDESK)
    desk.switch_page("app_pages/frontdesk_handoffs.py").run()
    assert not desk.exception, desk.exception
    assert desk.metric[0].value == "1"
    next(b for b in desk.button if b.label == "Claim").click().run()
    assert not desk.exception
    desk.get("button_group")[0].set_value("Claimed").run()  # claimed items leave "Open"
    assert not desk.exception, desk.exception
    desk.text_input(key=next(t.key for t in desk.text_input)).input("called back").run()
    next(b for b in desk.button if b.label == "Close").click().run()
    assert not desk.exception
    left = api_for(FRONTDESK).get("/chat/handoffs", status="open")
    assert left["open_total"] == 0


def test_ended_conversation_offers_a_new_one(ui: UI) -> None:
    at = _open_chat(ui(None))
    for _ in range(3):
        at = _send(at, "BUY FOLLOWERS")
    assert any("This conversation has ended" in i.value for i in at.info)
    next(b for b in at.button if b.label == "New conversation").click().run()
    assert not at.exception and at.get("chat_input")


def test_admin_knowledge_base_page(ui: UI) -> None:
    at = ui(ADMIN)
    at.switch_page("app_pages/admin_kb.py").run()
    assert not at.exception, at.exception
    assert [m.label for m in at.metric] == ["Documents", "Chunks", "Vector index"]
    assert at.metric[2].value == "In sync"
    at.text_input(key="kb_q").input("Dr Shetti timings")
    next(b for b in at.button if b.label == "Search").click().run()
    assert not at.exception, at.exception
    assert any("doctor match **Dr. Vikram Shetty**" in m.value for m in at.markdown)
    next(b for b in at.button if b.label == "Re-index changed documents").click().run()
    assert not at.exception, at.exception
