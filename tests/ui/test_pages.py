from __future__ import annotations

from collections.abc import Callable

import pytest
from fastapi.testclient import TestClient
from streamlit.testing.v1 import AppTest

from netracare.ui.api_client import ApiClient, ApiError
from netracare.ui.navigation import pages_for
from tests.fixtures_api import ADMIN, DOCTOR_EMAILS, FRONTDESK, PASSWORD, patient_email, pid

ROLE_EMAIL = {
    "patient": patient_email("P010"),
    "doctor": DOCTOR_EMAILS["D5"],
    "front_desk": FRONTDESK,
    "admin": ADMIN,
}


def _titles(role: str | None) -> list[str]:
    return [p.title for specs in pages_for(role).values() for p in specs]


def _text(at: AppTest) -> str:
    parts = [m.value for m in at.markdown] + [c.value for c in at.caption]
    parts += (
        [e.value for e in at.error] + [s.value for s in at.success] + [w.value for w in at.warning]
    )
    parts += [i.value for i in at.info]
    return "\n".join(str(p) for p in parts)


def test_navigation_per_role() -> None:
    assert _titles(None) == ["Sign in", "Register"]
    assert _titles("patient") == ["My appointments", "Book a visit", "Symptom check", "Messages"]
    assert _titles("doctor") == ["My schedule", "Visit notes"]
    assert "Alerts" in _titles("front_desk") and "AI usage" in _titles("admin")
    assert "Approvals" in _titles("front_desk") and "Audit log" not in _titles("front_desk")
    assert {"Approvals", "Audit log", "Schedules", "Users"} <= set(_titles("admin"))
    assert _titles("unknown") == ["Sign in"]
    for role in ("patient", "doctor", "front_desk", "admin"):
        defaults = [p for specs in pages_for(role).values() for p in specs if p.default]
        assert len(defaults) == 1


def test_signed_out_shows_login_and_disclaimer(ui: Callable[[str | None], AppTest]) -> None:
    at = ui(None)
    assert not at.exception
    assert at.title[0].value == "Sign in"
    assert "112" in _text(at)  # emergency guidance on every page


def test_login_flow(ui: Callable[[str | None], AppTest]) -> None:
    at = ui(None)
    at.text_input(key="login_email").input(FRONTDESK)
    at.text_input(key="login_password").input(PASSWORD)
    at.button[0].click().run()
    at.run()  # rerun after sign-in rebuilds navigation for the role
    assert not at.exception
    assert at.title[0].value == "Today"


def test_login_failure_shows_request_id(ui: Callable[[str | None], AppTest]) -> None:
    at = ui(None)
    at.text_input(key="login_email").input(FRONTDESK)
    at.text_input(key="login_password").input("wrong-password")
    at.button[0].click().run()
    assert not at.exception
    assert at.error and "reference" in at.error[0].value


ROLE_PAGES = [
    (role, spec.file)
    for role in ("patient", "doctor", "front_desk", "admin")
    for specs in pages_for(role).values()
    for spec in specs
]


@pytest.mark.parametrize(("role", "page"), ROLE_PAGES, ids=[f"{r}-{p}" for r, p in ROLE_PAGES])
def test_every_page_renders_for_its_role(
    ui: Callable[[str | None], AppTest], role: str, page: str
) -> None:
    at = ui(ROLE_EMAIL[role])
    assert not at.exception, at.exception
    at.switch_page(f"app_pages/{page}").run()
    assert not at.exception, at.exception
    assert not at.error, [e.value for e in at.error]


def test_booking_page_reads_back_then_books(
    ui: Callable[[str | None], AppTest], client: TestClient
) -> None:
    at = ui(ROLE_EMAIL["patient"])
    at.switch_page("app_pages/patient_book.py").run()
    assert not at.exception
    readback = next(m.value for m in at.markdown if m.value.startswith("**Please check:**"))
    assert "Dr." in readback and "with" in readback
    next(b for b in at.button if b.label == "Confirm booking").click().run()
    assert not at.exception
    assert at.success and "Booked" in at.success[0].value
    api: ApiClient = at.session_state["api_client"]
    mine = api.get("/appointments")
    assert mine["total"] == 1 and mine["items"][0]["patient"]["id"] == pid("P010")


def test_front_desk_approves_from_queue_page(
    ui: Callable[[str | None], AppTest], api_for: Callable[[str | None], ApiClient]
) -> None:
    patient = api_for(ROLE_EMAIL["patient"])
    avail = patient.get("/availability", specialty="CORNEA", type="surgery_consult")
    option = next(o for d in avail["days"] for o in d["options"])
    patient.post(
        "/appointments",
        {
            "patient_id": pid("P010"),
            "doctor_id": option["doctor_id"],
            "slot_ids": option["slot_ids"],
            "type": "surgery_consult",
        },
        idempotency_key="ui-test-approval-1",
    )
    at = ui(FRONTDESK)
    at.switch_page("app_pages/frontdesk_approvals.py").run()
    assert "New booking" in _text(at)
    approve = next(b for b in at.button if b.label == "Approve")
    approve.click().run()
    assert not at.exception
    assert patient.get("/appointments")["items"][0]["status"] == "confirmed"


def test_api_client_refreshes_once_then_signs_out(
    api_for: Callable[[str | None], ApiClient],
) -> None:
    api = api_for(ADMIN)
    api.access_token = "garbage"
    assert api.get("/auth/me")["role"] == "admin"  # silent refresh
    api.access_token = "garbage"
    api.refresh_token = "also-garbage-token"
    with pytest.raises(ApiError) as e:
        api.get("/auth/me")
    assert e.value.status == 401 and not api.is_authenticated


def test_api_client_unreachable() -> None:
    api = ApiClient("http://127.0.0.1:9", timeout=0.5)
    with pytest.raises(ApiError) as e:
        api.get("/health")
    assert e.value.code == "api_unreachable"


def test_api_client_logout_and_register(api_for: Callable[[str | None], ApiClient]) -> None:
    api = api_for(None)
    me = api.register(
        email="ui.new@example.com",
        password="Str0ng-pass!",
        full_name="UI New",
        dob="1991-02-03",
    )
    assert me["role"] == "patient" and api.is_authenticated
    api.logout()
    assert not api.is_authenticated
