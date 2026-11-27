"""Phase 4 UI: booking through the chat buttons; front-desk Edit on Approvals resumes the chat."""

from __future__ import annotations

from collections.abc import Callable

from streamlit.testing.v1 import AppTest

from netracare.llm.providers import FakeProvider
from netracare.ui.api_client import ApiClient
from tests.fixtures_api import FRONTDESK, guardian_email, patient_email
from tests.llm_helpers import ChatScript, route_json

UI = Callable[[str | None], AppTest]


def _chat(at: AppTest) -> AppTest:
    at.switch_page("app_pages/chat.py").run()
    assert not at.exception, at.exception
    return at


def _say(at: AppTest, text: str) -> AppTest:
    at.chat_input(key="chat_input").set_value(text).run()
    assert not at.exception, at.exception
    return at


def _click(at: AppTest, label_start: str) -> AppTest:
    next(b for b in at.button if b.label.startswith(label_start)).click().run()
    assert not at.exception, at.exception
    return at


def test_book_with_option_and_yes_buttons(ui: UI, use_llm: Callable[..., FakeProvider]) -> None:
    script = ChatScript()
    script.router.append(route_json("booking", "book with Dr Kavya Nair next friday"))
    use_llm(script)
    at = _say(
        _chat(ui(patient_email("P010"))), "book a check-up with Dr Kavya Nair next friday morning"
    )
    option_buttons = [b for b in at.button if b.label.startswith("Fri 20 Nov")]
    assert len(option_buttons) == 3
    option_buttons[1].click().run()
    assert not at.exception, at.exception
    assert any("Please confirm" in m.value for m in at.markdown)
    at = _click(at, "Yes, book it")
    assert any("is confirmed for" in m.value for m in at.markdown)


def test_front_desk_edits_a_chat_request(
    ui: UI, use_llm: Callable[..., FakeProvider], api_for: Callable[[str | None], ApiClient]
) -> None:
    script = ChatScript()
    script.router.append(route_json("booking", "book for my son"))
    use_llm(script)
    guardian = _chat(ui(guardian_email("P000")))
    _say(guardian, "book an eye check for my son")
    _say(guardian, "next tuesday")
    _click(guardian, "Tue 17 Nov")
    assert any("go to the front desk for approval" in c.value for c in guardian.caption)
    _click(guardian, "Yes, book it")
    assert any("Awaiting front desk" in m.value for m in guardian.markdown)  # st.badge

    desk = ui(FRONTDESK)
    desk.switch_page("app_pages/frontdesk_approvals.py").run()
    assert not desk.exception, desk.exception
    assert any("via chat" in m.value for m in desk.markdown)
    _click(desk, "Approve with this time")
    left = api_for(FRONTDESK).get("/approvals")
    assert not [r for r in left if r["source"] == "chat"]

    guardian.run()  # the patient reopens the chat
    assert any("different time" in m.value for m in guardian.markdown)
