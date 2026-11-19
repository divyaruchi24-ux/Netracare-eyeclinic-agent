"""POST /triage (public) and the front-desk alert workflow."""

from __future__ import annotations

from collections.abc import Callable

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from netracare.db.models import LLMCall, TriageAlert
from netracare.domain.enums import AlertStatus
from netracare.guardrails import scripts
from netracare.llm.providers import FakeProvider
from netracare.llm.types import LLMUnavailable
from tests.fixtures_api import Headers, assert_audited, fresh, pid
from tests.llm_helpers import resp, triage_json

UseLLM = Callable[..., FakeProvider]


def test_emergency_returns_verbatim_script_and_alerts(
    client: TestClient, db: Session, patient_headers: Headers
) -> None:
    r = client.post("/triage", json={"text": "my vision just went black"}, headers=patient_headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["level"] == "emergency" and body["codes"] == ["E1"]
    assert body["message"] == scripts.EMERGENCY_SCRIPT
    assert body["disclaimer"] == scripts.DISCLAIMERS["chat_footer"]
    alert = fresh(db).get(TriageAlert, body["alert_id"])
    assert alert is not None and alert.status is AlertStatus.OPEN
    assert alert.patient_id == pid("P010")  # a patient's own triage is linked to them
    assert_audited(db, "triage.alert", body["alert_id"])


def test_chemical_splash_gets_rinse_line(client: TestClient) -> None:
    text = "lol got bleach in my eye, anyway can I book tuesday"
    body = client.post("/triage", json={"text": text}).json()
    assert body["level"] == "emergency"
    assert body["message"] == scripts.emergency_message(["E4"])
    assert scripts.CHEMICAL_RINSE_LINE in body["message"]


def test_anonymous_allowed_and_not_linked_to_patient(client: TestClient, db: Session) -> None:
    body = client.post("/triage", json={"text": "sudden curtain over my vision"}).json()
    alert = fresh(db).get(TriageAlert, body["alert_id"])
    assert alert is not None and alert.patient_id is None and alert.actor_user_id is None


def test_urgent_script_and_retina_routing(client: TestClient) -> None:
    body = client.post("/triage", json={"text": "a few new floaters, no flashes"}).json()
    assert body["level"] == "urgent" and body["message"] == scripts.URGENT_SCRIPT
    assert body["specialty_code"] == "RETINA" and body["doctor_name"] == "Dr. Vikram Shetty"
    assert body["alert_id"] is not None


def test_routine_with_llm(client: TestClient, use_llm: UseLLM, db: Session) -> None:
    fake = use_llm([resp(triage_json("routine", specialty="GENERAL", concern="itchy eyes"))])
    body = client.post("/triage", json={"text": "my eyes itch a little"}).json()
    assert body["level"] == "routine" and body["source"] == "llm+rules"
    assert body["alert_id"] is None
    assert body["message"].endswith(scripts.CHECK_AVAILABILITY)
    assert fake.calls == 1
    row = fresh(db).scalars(select(LLMCall)).one()
    assert row.purpose == "triage" and row.prompt_id == "red_flag_classifier"


def test_llm_down_falls_back_no_500(client: TestClient, use_llm: UseLLM) -> None:
    use_llm([LLMUnavailable("down")] * 5)
    r = client.post("/triage", json={"text": "there's a grey shadow on the side of my vision"})
    assert r.status_code == 200
    assert r.json()["level"] == "emergency" and r.json()["source"] == "rules_fallback"


@pytest.mark.parametrize("text", ["   ", "👀👀👀", "?!"])
def test_empty_and_emoji_shortcircuit(client: TestClient, use_llm: UseLLM, text: str) -> None:
    fake = use_llm([resp(triage_json("emergency"))])
    body = client.post("/triage", json={"text": text}).json()
    assert body["level"] == "non_clinical" and body["source"] == "shortcircuit"
    assert fake.calls == 0


@pytest.mark.parametrize("text", ["", "x" * 2001])
def test_length_limits_422(client: TestClient, text: str) -> None:
    assert client.post("/triage", json={"text": text}).status_code == 422


def test_rate_limit_429(client: TestClient) -> None:
    for _ in range(10):
        assert client.post("/triage", json={"text": "hello"}).status_code == 200
    r = client.post("/triage", json={"text": "hello"})
    assert r.status_code == 429 and r.json()["error"]["code"] == "rate_limited"


def test_rate_limit_is_per_user(client: TestClient, patient_headers: Headers) -> None:
    for _ in range(10):
        client.post("/triage", json={"text": "hello"})
    assert client.post("/triage", json={"text": "hi"}, headers=patient_headers).status_code == 200


def test_patient_cannot_attach_other_patient(client: TestClient, patient_headers: Headers) -> None:
    r = client.post(
        "/triage", json={"text": "hello", "patient_id": pid("P011")}, headers=patient_headers
    )
    assert r.status_code == 404


def test_staff_can_attach_patient(
    client: TestClient, db: Session, frontdesk_headers: Headers
) -> None:
    body = client.post(
        "/triage",
        json={"text": "patient reports curtain over vision", "patient_id": pid("P011")},
        headers=frontdesk_headers,
    ).json()
    alert = fresh(db).get(TriageAlert, body["alert_id"])
    assert alert is not None and alert.patient_id == pid("P011")


def test_invalid_token_is_401_not_anonymous(client: TestClient) -> None:
    r = client.post("/triage", json={"text": "hi"}, headers={"Authorization": "Bearer nope"})
    assert r.status_code == 401


def _alert(client: TestClient) -> str:
    alert_id: str = client.post("/triage", json={"text": "bleach in my eye"}).json()["alert_id"]
    return alert_id


def test_alerts_list_open_first_with_counts(client: TestClient, frontdesk_headers: Headers) -> None:
    client.post("/triage", json={"text": "a few new floaters"})
    emergency = _alert(client)
    body = client.get("/triage/alerts", headers=frontdesk_headers).json()
    assert body["open_emergencies"] == 1 and body["open_total"] == 2
    assert body["items"][0]["id"] == emergency  # emergencies first
    assert "bleach" in body["items"][0]["excerpt_redacted"]
    only_open = client.get("/triage/alerts?status=open", headers=frontdesk_headers).json()
    assert len(only_open["items"]) == 2


def test_ack_then_double_ack_409_then_resolve(
    client: TestClient, db: Session, frontdesk_headers: Headers, admin_headers: Headers
) -> None:
    alert_id = _alert(client)
    r = client.post(f"/triage/alerts/{alert_id}/ack", headers=frontdesk_headers)
    assert r.status_code == 200 and r.json()["status"] == "acknowledged"
    again = client.post(f"/triage/alerts/{alert_id}/ack", headers=admin_headers)
    assert again.status_code == 409 and again.json()["error"]["code"] == "already_handled"
    r = client.post(
        f"/triage/alerts/{alert_id}/resolve",
        json={"note": "called, went to ER"},
        headers=admin_headers,
    )
    assert r.status_code == 200 and r.json()["status"] == "resolved"
    assert r.json()["resolution_note"] == "called, went to ER"
    assert_audited(db, "triage.ack", alert_id)
    assert_audited(db, "triage.resolve", alert_id)
    r = client.post(f"/triage/alerts/{alert_id}/resolve", json={"note": "x"}, headers=admin_headers)
    assert r.status_code == 409


def test_stale_version_ack_409(client: TestClient, frontdesk_headers: Headers) -> None:
    alert_id = _alert(client)
    r = client.post(f"/triage/alerts/{alert_id}/ack?expected_version=99", headers=frontdesk_headers)
    assert r.status_code == 409


def test_resolve_open_alert_directly(client: TestClient, frontdesk_headers: Headers) -> None:
    alert_id = _alert(client)
    r = client.post(
        f"/triage/alerts/{alert_id}/resolve", json={"note": "handled"}, headers=frontdesk_headers
    )
    assert r.json()["acknowledged_by"] is not None and r.json()["status"] == "resolved"


def test_unknown_alert_404(client: TestClient, frontdesk_headers: Headers) -> None:
    assert client.post("/triage/alerts/nope/ack", headers=frontdesk_headers).status_code == 404


def test_alerts_are_staff_only(
    client: TestClient, patient_headers: Headers, doctor_headers: Callable[[str], Headers]
) -> None:
    assert client.get("/triage/alerts").status_code == 401
    assert client.get("/triage/alerts", headers=patient_headers).status_code == 403
    assert client.get("/triage/alerts", headers=doctor_headers("D5")).status_code == 403
