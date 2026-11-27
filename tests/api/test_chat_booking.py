"""Booking through the chat API, and the front desk's approve / edit / reject resuming it."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from netracare.llm.providers import FakeProvider
from tests.fixtures_api import Headers, assert_audited, guardian_email, local, slot_ids_at
from tests.llm_helpers import ChatScript, route_json

UseLLM = Callable[..., FakeProvider]


def _say(client: TestClient, sid: str, text: str, h: Headers) -> dict[str, Any]:
    r = client.post(f"/chat/sessions/{sid}/messages", json={"text": text}, headers=h)
    assert r.status_code == 200, r.text
    body: dict[str, Any] = r.json()
    return body


def test_patient_books_through_chat(
    client: TestClient, patient_headers: Headers, use_llm: UseLLM
) -> None:
    script = ChatScript()
    script.router.append(route_json("booking", "book with Dr Kavya Nair next friday"))
    use_llm(script)
    sid = client.post("/chat/sessions", headers=patient_headers).json()["id"]
    t = _say(client, sid, "book a check-up with Dr Kavya Nair next friday morning", patient_headers)
    assert t["intent"] == "booking" and t["agent_step"] == "propose" and len(t["options"]) == 3
    opt = t["options"][1]
    assert opt["doctor_name"] == "Dr. Kavya Nair" and opt["id"]
    rb = _say(client, sid, f"option:{opt['id']}", patient_headers)
    assert rb["readback"] and rb["readback"]["needs_approval"] is False
    done = _say(client, sid, "yes", patient_headers)
    assert done["agent_step"] == "done" and "confirmed" in done["reply"]
    history = client.get(f"/chat/sessions/{sid}", headers=patient_headers).json()["messages"]
    assert history[1]["agent"]["options"][1]["id"] == opt["id"]
    mine = client.get("/appointments", headers=patient_headers).json()["items"]
    assert any(a["start_local"].startswith("2026-11-20T10:00") for a in mine)


def test_guardian_request_edited_by_front_desk_resumes_chat(
    client: TestClient,
    db: Session,
    login: Callable[..., Headers],
    frontdesk_headers: Headers,
    use_llm: UseLLM,
) -> None:
    script = ChatScript()
    script.router.append(route_json("booking", "book for my son"))
    use_llm(script)
    guardian = login(guardian_email("P000"))
    sid = client.post("/chat/sessions", headers=guardian).json()["id"]
    _say(client, sid, "book an eye check for my son", guardian)
    _say(client, sid, "next tuesday", guardian)
    rb = _say(client, sid, "first", guardian)
    assert "guardian_booking_minor" in rb["readback"]["reasons"]
    done = _say(client, sid, "yes", guardian)
    assert "front desk for approval" in done["reply"]

    queue = client.get("/approvals", headers=frontdesk_headers).json()
    req = next(r for r in queue if r["chat_session_id"] == sid)
    assert req["source"] == "chat"
    new_slots = slot_ids_at(db, "D4", local(19, 15))
    r = client.post(
        f"/approvals/{req['id']}/approve-with-changes",
        json={"slot_ids": new_slots, "note": "Thursday works better"},
        headers=frontdesk_headers,
    )
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "approved"
    assert r.json()["appointment"]["start_local"].startswith("2026-11-19T15:00")
    assert_audited(db, "approval.approve_with_changes", req["id"])
    texts = [
        m["text"] for m in client.get(f"/chat/sessions/{sid}", headers=guardian).json()["messages"]
    ]
    assert any("different time" in t and "Thursday 19 November 2026 at 15:00" in t for t in texts)
    again = client.post(
        f"/approvals/{req['id']}/approve-with-changes",
        json={"slot_ids": new_slots},
        headers=frontdesk_headers,
    )
    assert again.status_code == 409


def test_approve_with_changes_validation(
    client: TestClient, db: Session, patient_headers: Headers, frontdesk_headers: Headers
) -> None:
    from tests.fixtures_api import did, pid

    # A < 24 h cancellation request (cancel requests can't be edited)
    slots = slot_ids_at(db, "D5", local(10, 16))
    appt = client.post(
        "/appointments",
        json={
            "patient_id": pid("P010"),
            "doctor_id": did("D5"),
            "slot_ids": slots,
            "type": "routine",
        },
        headers={**patient_headers, "Idempotency-Key": "ui-awc-1"},
    ).json()["appointment"]
    cancel = client.post(f"/appointments/{appt['id']}/cancel", json={}, headers=patient_headers)
    req_id = cancel.json()["approval"]["id"]
    r = client.post(
        f"/approvals/{req_id}/approve-with-changes",
        json={"slot_ids": slots},
        headers=frontdesk_headers,
    )
    assert r.status_code == 422 and r.json()["error"]["code"] == "not_editable"
    assert (
        client.post(
            f"/approvals/{req_id}/approve-with-changes",
            json={"slot_ids": slots},
            headers=patient_headers,
        ).status_code
        == 403
    )


def test_anonymous_booking_request_keeps_the_booking_page_link(
    client: TestClient, use_llm: UseLLM
) -> None:
    script = ChatScript()
    script.router.append(route_json("booking", "book"))
    use_llm(script)
    s = client.post("/chat/sessions").json()
    t = _say(client, s["id"], "I want to book an appointment", {"X-Chat-Token": s["session_token"]})
    assert t["agent_step"] is None and t["suggested_action"] and t["options"] == []
