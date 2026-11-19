"""Visit-summary drafts: doctor-only drafting, faithfulness flags, patient sees approved only."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from netracare.db.models import NotificationOutbox, Patient
from netracare.guardrails.scripts import DISCLAIMERS
from netracare.llm.providers import FakeProvider
from netracare.llm.types import LLMUnavailable
from tests.fixtures_api import (
    Headers,
    assert_audited,
    booked,
    fresh,
    local,
    patient_email,
    pid,
    slot_ids_at,
)
from tests.llm_helpers import resp

UseLLM = Callable[..., FakeProvider]
DoctorHeaders = Callable[[str], Headers]


def notes_for(name: str) -> str:
    return (
        f"Patient {name} seen for blurry near vision. Vision 6/6 distance. IOP 14 mmHg OU. "
        "Mild dry eye. Start carboxymethylcellulose 0.5% 1 drop 4 times daily both eyes. "
        "Review in 3 months."
    )


GOOD_DRAFT: dict[str, Any] = {
    "what_we_checked": ["your distance vision", "your eye pressure"],
    "what_we_found": "Your distance vision is 6/6 and your eye pressure is 14 mmHg. Mild dry eye.",
    "next_steps": ["Use the eye drops as written below."],
    "medicines_mentioned": [
        {
            "name": "carboxymethylcellulose 0.5%",
            "instructions_verbatim": "1 drop 4 times daily both eyes",
        }
    ],
    "follow_up": "Review in 3 months.",
    "unclear_in_notes": [],
}
INVENTED = GOOD_DRAFT | {
    "what_we_found": "Eye pressure 22 mmHg, early glaucoma.",
    "medicines_mentioned": [{"name": "timolol", "instructions_verbatim": "twice daily"}],
}


@pytest.fixture
def visit(client: TestClient, db: Session, frontdesk_headers: Headers) -> str:
    """A checked-in appointment today with Dr. Kavya Nair (D5) for P010."""
    appt = booked(
        client, frontdesk_headers, pid("P010"), "D5", slot_ids_at(db, "D5", local(10, 10))
    )
    aid: str = appt["appointment"]["id"]
    assert (
        client.post(f"/appointments/{aid}/check-in", headers=frontdesk_headers).status_code == 200
    )
    return aid


@pytest.fixture
def name(db: Session) -> str:
    p = db.get(Patient, pid("P010"))
    assert p is not None
    return p.full_name


def _url(aid: str, suffix: str = "") -> str:
    return f"/appointments/{aid}/visit-summary{suffix}"


def test_full_flow_patient_404_until_approved(
    client: TestClient,
    db: Session,
    use_llm: UseLLM,
    visit: str,
    name: str,
    doctor_headers: DoctorHeaders,
    patient_headers: Headers,
) -> None:
    fake = use_llm([resp(GOOD_DRAFT)])
    d5 = doctor_headers("D5")
    r = client.post(_url(visit), json={"notes": notes_for(name)}, headers=d5)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "draft" and body["needs_attention"] == []
    assert body["disclaimer"] == DISCLAIMERS["summary_draft"]
    assert body["prompt_ref"] == "visit_summary@v1"
    # the patient's name never reached the model
    assert name.split()[0] not in fake.sent_text() and "the patient" in fake.sent_text()
    assert fake.requests[0].model == "openai/gpt-oss-120b"

    assert client.get(_url(visit), headers=patient_headers).status_code == 404
    r = client.post(_url(visit, "/approve"), headers=d5)
    assert r.status_code == 200 and r.json()["status"] == "approved"

    r = client.get(_url(visit), headers=patient_headers)
    assert r.status_code == 200
    out = r.json()
    assert (
        out["audience"] == "patient"
        and out["final"]["what_we_found"] == GOOD_DRAFT["what_we_found"]
    )
    assert out["notes"] is None and out["draft"] is None  # never the raw notes
    assert out["disclaimer"].startswith("Summary approved by Dr. Kavya Nair on 10 Nov 2026")
    assert_audited(db, "visit_summary.draft")
    assert_audited(db, "visit_summary.approve")
    n = fresh(db).scalar(
        select(func.count())
        .select_from(NotificationOutbox)
        .where(NotificationOutbox.template == "visit_summary_ready")
    )
    assert n == 1


def test_other_doctor_404_and_staff_status_only(
    client: TestClient,
    use_llm: UseLLM,
    visit: str,
    name: str,
    doctor_headers: DoctorHeaders,
    frontdesk_headers: Headers,
) -> None:
    use_llm([resp(GOOD_DRAFT)])
    assert (
        client.post(
            _url(visit), json={"notes": notes_for(name)}, headers=doctor_headers("D1")
        ).status_code
        == 404
    )
    client.post(_url(visit), json={"notes": notes_for(name)}, headers=doctor_headers("D5"))
    assert client.get(_url(visit), headers=doctor_headers("D1")).status_code == 404
    staff = client.get(_url(visit), headers=frontdesk_headers).json()
    assert staff["audience"] == "staff" and staff["status"] == "draft"
    assert staff["notes"] is None and staff["draft"] is None and staff["final"] is None


def test_other_patient_404(
    client: TestClient,
    login: Callable[..., Headers],
    use_llm: UseLLM,
    visit: str,
    name: str,
    doctor_headers: DoctorHeaders,
) -> None:
    use_llm([resp(GOOD_DRAFT)])
    d5 = doctor_headers("D5")
    client.post(_url(visit), json={"notes": notes_for(name)}, headers=d5)
    client.post(_url(visit, "/approve"), headers=d5)
    assert client.get(_url(visit), headers=login(patient_email("P011"))).status_code == 404


def test_invented_medicine_flagged_and_approve_blocked_until_acknowledged(
    client: TestClient, use_llm: UseLLM, visit: str, name: str, doctor_headers: DoctorHeaders
) -> None:
    use_llm([resp(INVENTED)])
    d5 = doctor_headers("D5")
    body = client.post(_url(visit), json={"notes": notes_for(name)}, headers=d5).json()
    flags = body["needs_attention"]
    assert "medicine not in notes: timolol" in flags
    assert "number not in notes: 22 mmHg" in flags
    assert "condition not in notes: glaucoma" in flags
    r = client.post(_url(visit, "/approve"), headers=d5)
    assert r.status_code == 409 and r.json()["error"]["code"] == "unresolved_flags"
    assert set(r.json()["error"]["details"]["unresolved"]) == set(flags)

    # acknowledging every flag (doctor confirms) unblocks approval
    r = client.patch(_url(visit), json={"draft": INVENTED, "acknowledged_flags": flags}, headers=d5)
    assert r.json()["unresolved_flags"] == []
    assert client.post(_url(visit, "/approve"), headers=d5).status_code == 200


def test_edit_clears_flags(
    client: TestClient,
    db: Session,
    use_llm: UseLLM,
    visit: str,
    name: str,
    doctor_headers: DoctorHeaders,
) -> None:
    use_llm([resp(INVENTED)])
    d5 = doctor_headers("D5")
    client.post(_url(visit), json={"notes": notes_for(name)}, headers=d5)
    r = client.patch(
        _url(visit), json={"draft": GOOD_DRAFT, "acknowledged_flags": ["bogus"]}, headers=d5
    )
    assert r.status_code == 200
    assert r.json()["needs_attention"] == [] and r.json()["acknowledged_flags"] == []
    assert_audited(db, "visit_summary.edit")
    assert client.post(_url(visit, "/approve"), headers=d5).status_code == 200


def test_llm_down_503_then_manual_draft_and_approve(
    client: TestClient,
    use_llm: UseLLM,
    visit: str,
    name: str,
    doctor_headers: DoctorHeaders,
    patient_headers: Headers,
) -> None:
    use_llm([LLMUnavailable("down")] * 3)
    d5 = doctor_headers("D5")
    r = client.post(_url(visit), json={"notes": notes_for(name)}, headers=d5)
    assert r.status_code == 503 and r.json()["error"]["code"] == "llm_unavailable"
    saved = client.get(_url(visit), headers=d5).json()
    assert saved["status"] == "draft" and saved["notes"].startswith("Patient")  # notes kept
    assert saved["draft"]["what_we_found"] == ""
    r = client.post(_url(visit, "/approve"), headers=d5)
    assert r.status_code == 422 and r.json()["error"]["code"] == "empty_summary"
    assert client.patch(_url(visit), json={"draft": GOOD_DRAFT}, headers=d5).status_code == 200
    assert client.post(_url(visit, "/approve"), headers=d5).status_code == 200
    assert client.get(_url(visit), headers=patient_headers).status_code == 200


def test_disabled_llm_is_also_503_manual(
    client: TestClient, visit: str, name: str, doctor_headers: DoctorHeaders
) -> None:
    r = client.post(_url(visit), json={"notes": notes_for(name)}, headers=doctor_headers("D5"))
    assert r.status_code == 503 and r.json()["error"]["details"] == {"manual_draft": True}


def test_approve_twice_409_and_no_redraft_or_discard(
    client: TestClient, use_llm: UseLLM, visit: str, name: str, doctor_headers: DoctorHeaders
) -> None:
    use_llm([resp(GOOD_DRAFT), resp(GOOD_DRAFT)])
    d5 = doctor_headers("D5")
    client.post(_url(visit), json={"notes": notes_for(name)}, headers=d5)
    assert client.post(_url(visit, "/approve"), headers=d5).status_code == 200
    again = client.post(_url(visit, "/approve"), headers=d5)
    assert again.status_code == 409 and again.json()["error"]["code"] == "already_approved"
    assert client.post(_url(visit), json={"notes": notes_for(name)}, headers=d5).status_code == 409
    assert client.post(_url(visit, "/discard"), headers=d5).status_code == 409
    assert client.patch(_url(visit), json={"draft": GOOD_DRAFT}, headers=d5).status_code == 409


def test_discard_then_approve_409_and_redraft_reopens(
    client: TestClient,
    db: Session,
    use_llm: UseLLM,
    visit: str,
    name: str,
    doctor_headers: DoctorHeaders,
) -> None:
    use_llm([resp(GOOD_DRAFT), resp(GOOD_DRAFT)])
    d5 = doctor_headers("D5")
    client.post(_url(visit), json={"notes": notes_for(name)}, headers=d5)
    assert client.post(_url(visit, "/discard"), headers=d5).json()["status"] == "discarded"
    assert_audited(db, "visit_summary.discard")
    assert client.post(_url(visit, "/approve"), headers=d5).status_code == 409
    assert (
        client.post(_url(visit), json={"notes": notes_for(name)}, headers=d5).json()["status"]
        == "draft"
    )


def test_visit_not_started_409_and_notes_length(
    client: TestClient, db: Session, frontdesk_headers: Headers, doctor_headers: DoctorHeaders
) -> None:
    appt = booked(
        client, frontdesk_headers, pid("P010"), "D5", slot_ids_at(db, "D5", local(13, 10))
    )
    aid = appt["appointment"]["id"]
    d5 = doctor_headers("D5")
    r = client.post(_url(aid), json={"notes": "x" * 40}, headers=d5)
    assert r.status_code == 409 and r.json()["error"]["code"] == "visit_not_started"
    assert client.post(_url(aid), json={"notes": "too short"}, headers=d5).status_code == 422


def test_get_missing_summary_404(
    client: TestClient,
    visit: str,
    doctor_headers: DoctorHeaders,
    frontdesk_headers: Headers,
    patient_headers: Headers,
) -> None:
    for h in (doctor_headers("D5"), frontdesk_headers, patient_headers):
        assert client.get(_url(visit), headers=h).status_code == 404
    assert (
        client.patch(
            _url(visit), json={"draft": GOOD_DRAFT}, headers=doctor_headers("D5")
        ).status_code
        == 404
    )


def test_invalid_draft_shape_422(
    client: TestClient, use_llm: UseLLM, visit: str, name: str, doctor_headers: DoctorHeaders
) -> None:
    use_llm([resp(GOOD_DRAFT)])
    d5 = doctor_headers("D5")
    client.post(_url(visit), json={"notes": notes_for(name)}, headers=d5)
    r = client.patch(_url(visit), json={"draft": {"what_we_found": 1}}, headers=d5)
    assert r.status_code == 422
