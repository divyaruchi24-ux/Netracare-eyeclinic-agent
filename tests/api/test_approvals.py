from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta
from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from netracare.db.models import AppointmentSlot, NotificationOutbox
from tests.fixtures_api import (
    FRONTDESK2,
    Headers,
    assert_audited,
    booked,
    fresh,
    guardian_email,
    local,
    patient_email,
    pid,
    slot_ids_at,
)
from tests.helpers import MutableClock

FRI_10 = local(13, 10)


def _pending_booking(client: TestClient, db: Session, headers: Headers) -> dict[str, Any]:
    slots = slot_ids_at(db, "D1", FRI_10, n=2)
    body = booked(client, headers, pid("P010"), "D1", slots, type="surgery_consult")
    return {"appt": body["appointment"], "req": body["appointment"]["approval"], "slots": slots}


def _holds(db: Session, slot_ids: list[str]) -> int:
    return int(
        fresh(db).scalar(
            select(func.count())
            .select_from(AppointmentSlot)
            .where(AppointmentSlot.slot_id.in_(slot_ids), AppointmentSlot.active)
        )
        or 0
    )


def _status(client: TestClient, headers: Headers, appt_id: str) -> str:
    status: str = client.get(f"/appointments/{appt_id}", headers=headers).json()["status"]
    return status


def test_list_pending_for_staff(
    client: TestClient, db: Session, patient_headers: Headers, frontdesk_headers: Headers
) -> None:
    p = _pending_booking(client, db, patient_headers)
    r = client.get("/approvals", headers=frontdesk_headers)
    assert r.status_code == 200
    items = r.json()
    assert [i["id"] for i in items] == [p["req"]["id"]]
    assert items[0]["reasons"] == ["new_patient_surgery_consult"]
    assert items[0]["appointment"]["patient"]["id"] == pid("P010")
    assert items[0]["is_overdue"] is False


def test_approve_new_booking(
    client: TestClient, db: Session, patient_headers: Headers, frontdesk_headers: Headers
) -> None:
    p = _pending_booking(client, db, patient_headers)
    r = client.post(
        f"/approvals/{p['req']['id']}/approve", json={"note": "ok"}, headers=frontdesk_headers
    )
    assert r.status_code == 200 and r.json()["status"] == "approved"
    assert _status(client, patient_headers, p["appt"]["id"]) == "confirmed"
    assert_audited(db, "approval.approve", p["req"]["id"])
    n = fresh(db).scalar(
        select(func.count())
        .select_from(NotificationOutbox)
        .where(NotificationOutbox.template == "approval_approved")
    )
    assert n == 1


def test_reject_new_booking_releases_slots(
    client: TestClient, db: Session, patient_headers: Headers, frontdesk_headers: Headers
) -> None:
    p = _pending_booking(client, db, patient_headers)
    r = client.post(f"/approvals/{p['req']['id']}/reject", json={}, headers=frontdesk_headers)
    assert r.json()["status"] == "rejected"
    assert _status(client, patient_headers, p["appt"]["id"]) == "rejected"
    assert _holds(db, p["slots"]) == 0
    assert_audited(db, "approval.reject", p["req"]["id"])


def test_double_decision_409(
    client: TestClient,
    db: Session,
    patient_headers: Headers,
    frontdesk_headers: Headers,
    login: Callable[..., Headers],
) -> None:
    p = _pending_booking(client, db, patient_headers)
    client.post(f"/approvals/{p['req']['id']}/approve", json={}, headers=frontdesk_headers)
    r = client.post(f"/approvals/{p['req']['id']}/reject", json={}, headers=login(FRONTDESK2))
    assert r.status_code == 409 and r.json()["error"]["code"] == "already_decided"


def test_patient_and_doctor_cannot_decide(
    client: TestClient,
    db: Session,
    patient_headers: Headers,
    doctor_headers: Callable[[str], Headers],
) -> None:
    p = _pending_booking(client, db, patient_headers)
    url = f"/approvals/{p['req']['id']}/approve"
    assert client.post(url, json={}, headers=patient_headers).status_code == 403
    assert client.post(url, json={}, headers=doctor_headers("D1")).status_code == 403
    assert client.get("/approvals", headers=patient_headers).status_code == 403


def test_cancel_request_approve_and_reject(
    client: TestClient,
    db: Session,
    patient_headers: Headers,
    frontdesk_headers: Headers,
    clock: MutableClock,
) -> None:
    slots = slot_ids_at(db, "D5", local(11, 9))
    appt = booked(client, patient_headers, pid("P010"), "D5", slots)["appointment"]
    clock.advance(timedelta(hours=1))
    req = client.post(
        f"/appointments/{appt['id']}/cancel", json={}, headers=patient_headers
    ).json()["approval"]
    r = client.post(f"/approvals/{req['id']}/reject", json={}, headers=frontdesk_headers)
    assert r.status_code == 200
    assert _status(client, patient_headers, appt["id"]) == "confirmed"  # refused: kept
    req2 = client.post(
        f"/appointments/{appt['id']}/cancel", json={}, headers=patient_headers
    ).json()["approval"]
    client.post(f"/approvals/{req2['id']}/approve", json={}, headers=frontdesk_headers)
    assert _status(client, patient_headers, appt["id"]) == "cancelled"
    assert _holds(db, slots) == 0


def _pending_reschedule(
    client: TestClient, db: Session, headers: Headers, clock: MutableClock
) -> dict[str, Any]:
    old_slots = slot_ids_at(db, "D5", local(11, 9))
    new_slots = slot_ids_at(db, "D5", FRI_10)
    old = booked(client, headers, pid("P010"), "D5", old_slots)["appointment"]
    clock.advance(timedelta(hours=1))
    r = client.post(
        f"/appointments/{old['id']}/reschedule", json={"slot_ids": new_slots}, headers=headers
    ).json()
    return {
        "old": old,
        "new": r["appointment"],
        "req": r["appointment"]["approval"],
        "old_slots": old_slots,
        "new_slots": new_slots,
    }


def test_reschedule_request_approve(
    client: TestClient,
    db: Session,
    patient_headers: Headers,
    frontdesk_headers: Headers,
    clock: MutableClock,
) -> None:
    p = _pending_reschedule(client, db, patient_headers, clock)
    assert p["req"]["kind"] == "reschedule" and p["req"]["is_target"] is True
    listed = client.get("/approvals", headers=frontdesk_headers).json()[0]
    assert listed["proposed"]["id"] == p["new"]["id"]
    client.post(f"/approvals/{p['req']['id']}/approve", json={}, headers=frontdesk_headers)
    assert _status(client, patient_headers, p["old"]["id"]) == "rescheduled"
    assert _status(client, patient_headers, p["new"]["id"]) == "confirmed"
    assert _holds(db, p["old_slots"]) == 0 and _holds(db, p["new_slots"]) == 1


def test_reschedule_request_reject_keeps_original(
    client: TestClient,
    db: Session,
    patient_headers: Headers,
    frontdesk_headers: Headers,
    clock: MutableClock,
) -> None:
    p = _pending_reschedule(client, db, patient_headers, clock)
    client.post(f"/approvals/{p['req']['id']}/reject", json={}, headers=frontdesk_headers)
    assert _status(client, patient_headers, p["old"]["id"]) == "confirmed"
    assert _status(client, patient_headers, p["new"]["id"]) == "rejected"
    assert _holds(db, p["old_slots"]) == 1 and _holds(db, p["new_slots"]) == 0


def test_withdraw_by_patient(
    client: TestClient,
    db: Session,
    patient_headers: Headers,
    frontdesk_headers: Headers,
    login: Callable[..., Headers],
    clock: MutableClock,
) -> None:
    p = _pending_reschedule(client, db, patient_headers, clock)
    stranger = login(patient_email("P011"))
    assert client.post(f"/approvals/{p['req']['id']}/withdraw", headers=stranger).status_code == 404
    assert (
        client.post(f"/approvals/{p['req']['id']}/withdraw", headers=frontdesk_headers).status_code
        == 403
    )
    r = client.post(f"/approvals/{p['req']['id']}/withdraw", headers=patient_headers)
    assert r.status_code == 200 and r.json()["status"] == "withdrawn"
    assert _status(client, patient_headers, p["new"]["id"]) == "cancelled"
    assert _status(client, patient_headers, p["old"]["id"]) == "confirmed"
    assert_audited(db, "approval.withdraw", p["req"]["id"])


def test_withdraw_new_booking(client: TestClient, db: Session, patient_headers: Headers) -> None:
    p = _pending_booking(client, db, patient_headers)
    client.post(f"/approvals/{p['req']['id']}/withdraw", headers=patient_headers)
    assert _status(client, patient_headers, p["appt"]["id"]) == "cancelled"
    assert _holds(db, p["slots"]) == 0


def test_staff_cancel_of_original_withdraws_pending_reschedule(
    client: TestClient,
    db: Session,
    patient_headers: Headers,
    frontdesk_headers: Headers,
    clock: MutableClock,
) -> None:
    p = _pending_reschedule(client, db, patient_headers, clock)
    client.post(f"/appointments/{p['old']['id']}/cancel", json={}, headers=frontdesk_headers)
    assert _status(client, patient_headers, p["new"]["id"]) == "cancelled"
    assert _holds(db, p["new_slots"]) == 0
    r = client.get(f"/approvals/{p['req']['id']}", headers=patient_headers)
    assert r.json()["status"] == "withdrawn"


def test_overdue_flag_after_4h(
    client: TestClient,
    db: Session,
    patient_headers: Headers,
    frontdesk_headers: Headers,
    clock: MutableClock,
) -> None:
    _pending_booking(client, db, patient_headers)
    clock.advance(timedelta(hours=5))
    items = client.get("/approvals", headers=frontdesk_headers).json()
    assert items[0]["is_overdue"] is True
    q = client.get("/front-desk/queue", params={"date": "2026-11-10"}, headers=frontdesk_headers)
    assert q.json()["overdue_approvals"] == 1


def test_approve_after_start_time_409(
    client: TestClient,
    db: Session,
    patient_headers: Headers,
    frontdesk_headers: Headers,
    clock: MutableClock,
) -> None:
    p = _pending_booking(client, db, patient_headers)
    clock.set(local(13, 10, 1))
    r = client.post(f"/approvals/{p['req']['id']}/approve", json={}, headers=frontdesk_headers)
    assert r.status_code == 409 and r.json()["error"]["code"] == "appointment_in_past"


def test_get_approval_scoped(
    client: TestClient,
    db: Session,
    patient_headers: Headers,
    login: Callable[..., Headers],
) -> None:
    p = _pending_booking(client, db, patient_headers)
    assert client.get(f"/approvals/{p['req']['id']}", headers=patient_headers).status_code == 200
    stranger = login(guardian_email("P002"))
    assert client.get(f"/approvals/{p['req']['id']}", headers=stranger).status_code == 404
    assert client.get("/approvals/nope", headers=patient_headers).status_code == 404


def test_list_all_statuses(
    client: TestClient, db: Session, patient_headers: Headers, frontdesk_headers: Headers
) -> None:
    p = _pending_booking(client, db, patient_headers)
    client.post(f"/approvals/{p['req']['id']}/approve", json={}, headers=frontdesk_headers)
    assert client.get("/approvals", headers=frontdesk_headers).json() == []
    approved = client.get(
        "/approvals", params={"status": "approved"}, headers=frontdesk_headers
    ).json()
    assert len(approved) == 1 and approved[0]["decided_at"] is not None
