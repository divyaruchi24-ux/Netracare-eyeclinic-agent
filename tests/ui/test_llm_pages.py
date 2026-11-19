"""Phase 2 UI: symptom check, booking pause, doctor visit notes, eye-fact banner."""

from __future__ import annotations

from collections.abc import Callable

import streamlit as st
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session
from streamlit.testing.v1 import AppTest

from netracare.guardrails import scripts
from netracare.llm.providers import FakeProvider
from netracare.ui.api_client import ApiClient
from netracare.ui.components import md_lines
from tests.fixtures_api import DOCTOR_EMAILS, FRONTDESK, did, local, patient_email, pid, slot_ids_at
from tests.llm_helpers import resp

UI = Callable[[str | None], AppTest]


def test_banner_on_login_page_with_disclaimer(ui: UI) -> None:
    st.cache_data.clear()
    at = ui(None)
    assert not at.exception
    assert any(i.value.startswith("**Did you know?**") for i in at.info)
    assert any(c.value == scripts.DISCLAIMERS["eye_fact"] for c in at.caption)


def test_symptom_check_emergency_shows_exact_script(ui: UI) -> None:
    at = ui(patient_email("P010"))
    at.switch_page("app_pages/patient_symptom_check.py").run()
    at.text_area(key="symptom_text").input("lol got bleach in my eye, anyway can I book tuesday")
    next(b for b in at.button if b.label == "Check").click().run()
    assert not at.exception
    assert at.error[0].value == md_lines(scripts.emergency_message(["E4"]))
    assert not any(getattr(p, "label", "") == "Book a visit" for p in at.get("page_link"))


def test_symptom_check_urgent_warning(ui: UI) -> None:
    at = ui(patient_email("P010"))
    at.switch_page("app_pages/patient_symptom_check.py").run()
    at.text_area(key="symptom_text").input("a few new floaters, no flashes")
    next(b for b in at.button if b.label == "Check").click().run()
    assert at.warning[0].value == md_lines(scripts.URGENT_SCRIPT)


def test_alerts_page_lists_and_acknowledges(ui: UI, client: TestClient) -> None:
    client.post("/triage", json={"text": "bleach in my eye"})
    at = ui(FRONTDESK)
    at.switch_page("app_pages/frontdesk_alerts.py").run()
    assert not at.exception
    assert at.metric[0].value == "1"
    next(b for b in at.button if b.label == "Acknowledge").click().run()
    assert not at.exception
    assert client.get("/triage/alerts", headers={}).status_code == 401  # sanity: staff only


def test_doctor_draft_edit_approve_flow(
    ui: UI,
    client: TestClient,
    db: Session,
    use_llm: Callable[..., FakeProvider],
    api_for: Callable[[str | None], ApiClient],
) -> None:
    desk = api_for(FRONTDESK)
    appt = desk.post(
        "/appointments",
        {
            "patient_id": pid("P010"),
            "doctor_id": did("D5"),
            "slot_ids": slot_ids_at(db, "D5", local(10, 10)),
            "type": "routine",
        },
        idempotency_key="ui-visit-1",
    )
    aid = appt["appointment"]["id"]
    desk.post(f"/appointments/{aid}/check-in")
    draft = {
        "what_we_checked": ["vision"],
        "what_we_found": "Vision is 6/6 in both eyes.",
        "next_steps": [],
        "medicines_mentioned": [{"name": "timolol", "instructions_verbatim": "twice daily"}],
        "follow_up": None,
        "unclear_in_notes": [],
    }
    use_llm([resp(draft)])
    at = ui(DOCTOR_EMAILS["D5"])
    at.switch_page("app_pages/doctor_visit_notes.py").run()
    assert not at.exception
    at.text_area(key=f"vn_notes_{aid}").input("Vision 6/6 both eyes. No medicines started today.")
    next(b for b in at.button if b.label == "Generate draft").click().run()
    assert not at.exception
    assert any("DRAFT" in w.value for w in at.warning)
    flags = [c for c in at.checkbox if c.label.startswith(":red[")]
    assert flags and "timolol" in flags[0].label
    # approve is blocked until the flag is acknowledged
    next(b for b in at.button if b.label == "Approve and share").click().run()
    assert any("flagged" in e.value.lower() or "acknowledge" in e.value.lower() for e in at.error)
    for c in [c for c in at.checkbox if c.label.startswith(":red[")]:
        c.check()
    next(b for b in at.button if b.label == "Save changes").click().run()
    next(b for b in at.button if b.label == "Approve and share").click().run()
    assert not at.exception
    patient = api_for(patient_email("P010"))
    assert patient.get(f"/appointments/{aid}/visit-summary")["status"] == "approved"
    at2 = ui(patient_email("P010"))
    assert not at2.exception
    assert any(e.label == "Visit summary" for e in at2.expander)


def test_booking_widget_pauses_on_emergency(ui: UI) -> None:
    at = ui(patient_email("P010"))
    at.switch_page("app_pages/patient_book.py").run()
    at.text_input[0].input("vision went black suddenly")
    next(b for b in at.button if b.label == "Confirm booking").click().run()
    assert not at.exception
    assert at.error and at.error[0].value == md_lines(scripts.EMERGENCY_SCRIPT)
