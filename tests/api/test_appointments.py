"""Booking / cancel / reschedule / visit-day behaviour through the API.

Clock: Tue 2026-11-10 09:00 IST. Dr. Nair (D5) works Mon-Wed, Fri, Sat; Dr. Rao (D1) has no
Wednesday OPD; Dr. Menon (D4) is pediatrics.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from netracare.db.models import (
    Appointment,
    AppointmentSlot,
    ApprovalRequest,
    AuditLog,
    Doctor,
    NotificationOutbox,
    Patient,
    Slot,
)
from netracare.domain.enums import SlotStatus
from netracare.services.notification_service import NotificationService
from tests.fixtures_api import (
    FRONTDESK,
    Headers,
    assert_audited,
    book_api,
    booked,
    did,
    fresh,
    guardian_email,
    local,
    patient_email,
    pid,
    slot_ids_at,
)
from tests.helpers import MutableClock

FRI_10 = local(13, 10)


def _outbox(db: Session, patient_id: str, template: str) -> int:
    fresh(db)
    return int(
        db.scalar(
            select(func.count())
            .select_from(NotificationOutbox)
            .where(
                NotificationOutbox.to_patient_id == patient_id,
                NotificationOutbox.template == template,
            )
        )
        or 0
    )


def _active_holds(db: Session, slot_ids: list[str]) -> int:
    fresh(db)
    return int(
        db.scalar(
            select(func.count())
            .select_from(AppointmentSlot)
            .where(AppointmentSlot.slot_id.in_(slot_ids), AppointmentSlot.active)
        )
        or 0
    )


# -- booking --------------------------------------------------------------------------------


def test_book_routine_auto_confirms(
    client: TestClient, db: Session, patient_headers: Headers
) -> None:
    slots = slot_ids_at(db, "D5", FRI_10)
    body = booked(client, patient_headers, pid("P010"), "D5", slots)
    appt = body["appointment"]
    assert appt["status"] == "confirmed" and body["requires_approval"] is False
    assert appt["start_local"].startswith("2026-11-13T10:00:00+05:30")
    assert appt["doctor"]["name"].startswith("Dr.")
    assert _active_holds(db, slots) == 1
    assert _outbox(db, pid("P010"), "appointment_confirmed") == 1
    row = assert_audited(db, "appointment.create", appt["id"])
    assert row.request_id  # request id propagated into the audit trail


def test_booked_slot_disappears_from_availability(
    client: TestClient, db: Session, patient_headers: Headers
) -> None:
    slots = slot_ids_at(db, "D5", FRI_10)
    booked(client, patient_headers, pid("P010"), "D5", slots)
    r = client.get(
        "/availability",
        params={"doctor_id": did("D5"), "from_date": "2026-11-13", "to_date": "2026-11-13"},
        headers=patient_headers,
    )
    ids = {sid for d in r.json()["days"] for o in d["options"] for sid in o["slot_ids"]}
    assert slots[0] not in ids and len(ids) > 10


def test_surgery_consult_new_patient_needs_approval(
    client: TestClient, db: Session, patient_headers: Headers
) -> None:
    slots = slot_ids_at(db, "D1", FRI_10, n=2)
    body = booked(client, patient_headers, pid("P010"), "D1", slots, type="surgery_consult")
    assert body["appointment"]["status"] == "pending_approval"
    assert body["approval_reasons"] == ["new_patient_surgery_consult"]
    assert body["appointment"]["approval"]["kind"] == "new_booking"
    assert _outbox(db, pid("P010"), "appointment_pending_approval") == 1
    assert _active_holds(db, slots) == 2  # pending bookings hold their slots


def test_guardian_booking_for_minor_needs_approval(
    client: TestClient, db: Session, login: Callable[..., Headers]
) -> None:
    headers = login(guardian_email("P000"))
    slots = slot_ids_at(db, "D4", local(11, 10))
    body = booked(client, headers, pid("P000"), "D4", slots)
    assert body["appointment"]["status"] == "pending_approval"
    assert "guardian_booking_minor" in body["approval_reasons"]


def test_no_show_flagged_patient_needs_approval(
    client: TestClient, db: Session, login: Callable[..., Headers]
) -> None:
    headers = login(patient_email("P006"))  # seeded with 3 no-shows
    body = booked(client, headers, pid("P006"), "D5", slot_ids_at(db, "D5", FRI_10))
    assert body["approval_reasons"] == ["no_show_flagged"]


def test_staff_override_skips_approval_and_is_audited(
    client: TestClient, db: Session, frontdesk_headers: Headers
) -> None:
    slots = slot_ids_at(db, "D1", FRI_10, n=2)
    body = booked(
        client,
        frontdesk_headers,
        pid("P010"),
        "D1",
        slots,
        type="surgery_consult",
        override=True,
        override_reason="referred by Dr. Rao, urgent work-up",
    )
    assert body["appointment"]["status"] == "confirmed"
    assert body["approval_reasons"] == ["new_patient_surgery_consult"]  # recorded, not enforced
    row = assert_audited(db, "appointment.override", body["appointment"]["id"])
    assert row.reason == "referred by Dr. Rao, urgent work-up"


def test_patient_cannot_override(client: TestClient, db: Session, patient_headers: Headers) -> None:
    r = book_api(
        client,
        patient_headers,
        pid("P010"),
        "D5",
        slot_ids_at(db, "D5", FRI_10),
        override=True,
        override_reason="please",
    )
    assert r.status_code == 403 and r.json()["error"]["code"] == "override_forbidden"


def test_override_without_reason_422(
    client: TestClient, db: Session, frontdesk_headers: Headers
) -> None:
    r = book_api(
        client, frontdesk_headers, pid("P010"), "D5", slot_ids_at(db, "D5", FRI_10), override=True
    )
    assert r.status_code == 422


@pytest.mark.parametrize(
    ("make_slots", "doctor", "appt_type", "code"),
    [
        (lambda db: slot_ids_at(db, "D5", local(10, 9)), "D5", "routine", "slot_in_past"),
        (lambda db: slot_ids_at(db, "D1", FRI_10), "D5", "routine", "slot_doctor_mismatch"),
        (
            lambda db: slot_ids_at(db, "D1", FRI_10) + slot_ids_at(db, "D1", local(13, 10, 30)),
            "D1",
            "new_patient",
            "slots_not_consecutive",
        ),
        (lambda db: slot_ids_at(db, "D5", FRI_10, n=2), "D5", "routine", "wrong_slot_count"),
        (lambda db: slot_ids_at(db, "D1", FRI_10), "D1", "surgery_consult", "wrong_slot_count"),
        (lambda db: ["no-such-slot"], "D5", "routine", "slot_not_found"),
    ],
)
def test_invalid_slots_422(
    client: TestClient,
    db: Session,
    patient_headers: Headers,
    make_slots: Callable[[Session], list[str]],
    doctor: str,
    appt_type: str,
    code: str,
) -> None:
    r = book_api(client, patient_headers, pid("P010"), doctor, make_slots(db), type=appt_type)
    assert r.status_code == 422, r.text
    assert r.json()["error"]["code"] == code


def test_duplicate_slot_ids_422(client: TestClient, db: Session, patient_headers: Headers) -> None:
    s = slot_ids_at(db, "D1", FRI_10)
    r = book_api(client, patient_headers, pid("P010"), "D1", s + s, type="new_patient")
    assert r.status_code == 422 and r.json()["error"]["code"] == "duplicate_slot_ids"


def test_blocked_slot_409(client: TestClient, db: Session, patient_headers: Headers) -> None:
    slots = slot_ids_at(db, "D5", FRI_10)
    slot = db.get(Slot, slots[0])
    assert slot is not None
    slot.status = SlotStatus.BLOCKED
    db.commit()
    r = book_api(client, patient_headers, pid("P010"), "D5", slots)
    assert r.status_code == 409 and r.json()["error"]["code"] == "slot_unavailable"


def test_inactive_doctor_422(client: TestClient, db: Session, patient_headers: Headers) -> None:
    doc = db.get(Doctor, did("D5"))
    assert doc is not None
    doc.is_active = False
    db.commit()
    r = book_api(client, patient_headers, pid("P010"), "D5", slot_ids_at(db, "D5", FRI_10))
    assert r.status_code == 422 and r.json()["error"]["code"] == "doctor_inactive"


def test_unknown_doctor_404(client: TestClient, db: Session, patient_headers: Headers) -> None:
    body = {
        "patient_id": pid("P010"),
        "doctor_id": "nope",
        "slot_ids": slot_ids_at(db, "D5", FRI_10),
        "type": "routine",
    }
    r = client.post(
        "/appointments", json=body, headers={**patient_headers, "Idempotency-Key": "abcdefgh1"}
    )
    assert r.status_code == 404


def test_slot_already_taken_409(
    client: TestClient, db: Session, patient_headers: Headers, login: Callable[..., Headers]
) -> None:
    slots = slot_ids_at(db, "D5", FRI_10)
    booked(client, patient_headers, pid("P010"), "D5", slots)
    r = book_api(client, login(patient_email("P011")), pid("P011"), "D5", slots)
    assert r.status_code == 409 and r.json()["error"]["code"] == "slot_unavailable"


def test_duplicate_same_doctor_same_day_409(
    client: TestClient, db: Session, patient_headers: Headers
) -> None:
    booked(client, patient_headers, pid("P010"), "D5", slot_ids_at(db, "D5", FRI_10))
    r = book_api(client, patient_headers, pid("P010"), "D5", slot_ids_at(db, "D5", local(13, 15)))
    assert r.status_code == 409 and r.json()["error"]["code"] == "duplicate_booking"


def test_overlap_with_other_doctor_409(
    client: TestClient, db: Session, patient_headers: Headers
) -> None:
    booked(client, patient_headers, pid("P010"), "D5", slot_ids_at(db, "D5", FRI_10))
    r = book_api(client, patient_headers, pid("P010"), "D1", slot_ids_at(db, "D1", FRI_10))
    assert r.status_code == 409 and r.json()["error"]["code"] == "overlapping_appointment"


def test_patient_cannot_book_for_stranger_404(
    client: TestClient, db: Session, patient_headers: Headers
) -> None:
    r = book_api(client, patient_headers, pid("P011"), "D5", slot_ids_at(db, "D5", FRI_10))
    assert r.status_code == 404


def test_doctor_cannot_book_403(
    client: TestClient, db: Session, doctor_headers: Callable[[str], Headers]
) -> None:
    r = book_api(client, doctor_headers("D5"), pid("P010"), "D5", slot_ids_at(db, "D5", FRI_10))
    assert r.status_code == 403


def test_missing_idempotency_key_422(
    client: TestClient, db: Session, patient_headers: Headers
) -> None:
    body = {
        "patient_id": pid("P010"),
        "doctor_id": did("D5"),
        "slot_ids": slot_ids_at(db, "D5", FRI_10),
        "type": "routine",
    }
    r = client.post("/appointments", json=body, headers=patient_headers)
    assert r.status_code == 422


def test_reason_too_long_422(client: TestClient, db: Session, patient_headers: Headers) -> None:
    r = book_api(
        client, patient_headers, pid("P010"), "D5", slot_ids_at(db, "D5", FRI_10), reason="x" * 501
    )
    assert r.status_code == 422


def test_failure_mid_transaction_rolls_back_everything(
    app: object, db: Session, patient_headers: Headers, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*_: object, **__: object) -> None:
        raise RuntimeError("outbox down")

    monkeypatch.setattr(NotificationService, "enqueue", boom)
    from fastapi import FastAPI

    assert isinstance(app, FastAPI)
    c = TestClient(app, raise_server_exceptions=False)
    slots = slot_ids_at(db, "D5", FRI_10)
    r = book_api(c, patient_headers, pid("P010"), "D5", slots)
    assert r.status_code == 500
    fresh(db)
    assert db.scalar(select(func.count()).select_from(Appointment)) == 0
    assert _active_holds(db, slots) == 0
    assert (
        db.scalar(
            select(func.count())
            .select_from(AuditLog)
            .where(AuditLog.action == "appointment.create")
        )
        == 0
    )


# -- cancellation ---------------------------------------------------------------------------


def test_cancel_more_than_24h_is_immediate_and_frees_slot(
    client: TestClient, db: Session, patient_headers: Headers, login: Callable[..., Headers]
) -> None:
    slots = slot_ids_at(db, "D5", FRI_10)
    appt = booked(client, patient_headers, pid("P010"), "D5", slots)["appointment"]
    r = client.post(f"/appointments/{appt['id']}/cancel", json={}, headers=patient_headers)
    assert r.status_code == 200
    assert r.json()["cancelled"] is True and r.json()["appointment"]["status"] == "cancelled"
    assert _active_holds(db, slots) == 0
    assert _outbox(db, pid("P010"), "appointment_cancelled") == 1
    assert_audited(db, "appointment.cancel", appt["id"])
    booked(client, login(patient_email("P011")), pid("P011"), "D5", slots)  # rebookable


def test_cancel_exactly_24h_is_free(
    client: TestClient, db: Session, patient_headers: Headers
) -> None:
    appt = booked(client, patient_headers, pid("P010"), "D5", slot_ids_at(db, "D5", local(11, 9)))
    r = client.post(
        f"/appointments/{appt['appointment']['id']}/cancel", json={}, headers=patient_headers
    )
    assert r.json()["cancelled"] is True


def test_cancel_within_24h_by_patient_creates_request(
    client: TestClient, db: Session, patient_headers: Headers, clock: MutableClock
) -> None:
    slots = slot_ids_at(db, "D5", local(11, 9))
    appt = booked(client, patient_headers, pid("P010"), "D5", slots)["appointment"]
    clock.advance(timedelta(minutes=1))  # 23h59m before the visit
    r = client.post(
        f"/appointments/{appt['id']}/cancel", json={"reason": "travel"}, headers=patient_headers
    )
    body = r.json()
    assert r.status_code == 200 and body["cancelled"] is False
    assert body["appointment"]["status"] == "confirmed"
    assert body["approval"]["kind"] == "cancel" and body["approval"]["status"] == "pending"
    assert _active_holds(db, slots) == 1
    assert_audited(db, "appointment.cancel_requested", appt["id"])
    again = client.post(f"/appointments/{appt['id']}/cancel", json={}, headers=patient_headers)
    assert again.status_code == 409 and again.json()["error"]["code"] == "change_already_requested"


def test_cancel_within_24h_by_staff_is_immediate(
    client: TestClient,
    db: Session,
    patient_headers: Headers,
    frontdesk_headers: Headers,
    clock: MutableClock,
) -> None:
    appt = booked(client, patient_headers, pid("P010"), "D5", slot_ids_at(db, "D5", local(11, 9)))
    clock.advance(timedelta(hours=2))
    r = client.post(
        f"/appointments/{appt['appointment']['id']}/cancel",
        json={"reason": "patient phoned in"},
        headers=frontdesk_headers,
    )
    assert r.json()["cancelled"] is True


def test_cancel_pending_booking_withdraws_its_request(
    client: TestClient, db: Session, patient_headers: Headers
) -> None:
    slots = slot_ids_at(db, "D1", FRI_10, n=2)
    appt = booked(client, patient_headers, pid("P010"), "D1", slots, type="surgery_consult")
    appt_id = appt["appointment"]["id"]
    r = client.post(f"/appointments/{appt_id}/cancel", json={}, headers=patient_headers)
    assert r.json()["cancelled"] is True
    fresh(db)
    req = db.scalar(select(ApprovalRequest).where(ApprovalRequest.appointment_id == appt_id))
    assert req is not None and req.status.value == "withdrawn"
    assert _active_holds(db, slots) == 0


def test_cancel_twice_409(client: TestClient, db: Session, patient_headers: Headers) -> None:
    appt = booked(client, patient_headers, pid("P010"), "D5", slot_ids_at(db, "D5", FRI_10))
    url = f"/appointments/{appt['appointment']['id']}/cancel"
    client.post(url, json={}, headers=patient_headers)
    r = client.post(url, json={}, headers=patient_headers)
    assert r.status_code == 409 and r.json()["error"]["code"] == "invalid_transition"


def test_cancel_started_appointment_409(
    client: TestClient, db: Session, frontdesk_headers: Headers, clock: MutableClock
) -> None:
    appt = booked(
        client, frontdesk_headers, pid("P010"), "D5", slot_ids_at(db, "D5", local(10, 10))
    )
    clock.set(local(10, 10, 5))
    r = client.post(
        f"/appointments/{appt['appointment']['id']}/cancel", json={}, headers=frontdesk_headers
    )
    assert r.status_code == 409 and r.json()["error"]["code"] == "appointment_in_past"


def test_doctor_cannot_cancel_403(
    client: TestClient,
    db: Session,
    patient_headers: Headers,
    doctor_headers: Callable[[str], Headers],
) -> None:
    appt = booked(client, patient_headers, pid("P010"), "D5", slot_ids_at(db, "D5", FRI_10))
    r = client.post(
        f"/appointments/{appt['appointment']['id']}/cancel", json={}, headers=doctor_headers("D5")
    )
    assert r.status_code == 403


# -- rescheduling ---------------------------------------------------------------------------


def test_reschedule_more_than_24h_is_single_step(
    client: TestClient, db: Session, patient_headers: Headers
) -> None:
    old_slots = slot_ids_at(db, "D5", FRI_10)
    new_slots = slot_ids_at(db, "D5", local(13, 11))
    old = booked(client, patient_headers, pid("P010"), "D5", old_slots)["appointment"]
    r = client.post(
        f"/appointments/{old['id']}/reschedule",
        json={"slot_ids": new_slots},
        headers=patient_headers,
    )
    assert r.status_code == 200, r.text
    new = r.json()["appointment"]
    assert new["status"] == "confirmed" and new["rescheduled_from_id"] == old["id"]
    old_now = client.get(f"/appointments/{old['id']}", headers=patient_headers).json()
    assert old_now["status"] == "rescheduled"
    assert _active_holds(db, old_slots) == 0 and _active_holds(db, new_slots) == 1
    assert _outbox(db, pid("P010"), "appointment_rescheduled") == 1


def test_reschedule_within_24h_needs_approval_old_kept(
    client: TestClient, db: Session, patient_headers: Headers, clock: MutableClock
) -> None:
    old_slots = slot_ids_at(db, "D5", local(11, 9))
    new_slots = slot_ids_at(db, "D5", FRI_10)
    old = booked(client, patient_headers, pid("P010"), "D5", old_slots)["appointment"]
    clock.advance(timedelta(hours=3))
    r = client.post(
        f"/appointments/{old['id']}/reschedule",
        json={"slot_ids": new_slots},
        headers=patient_headers,
    )
    body = r.json()
    assert body["requires_approval"] is True
    assert body["approval_reasons"] == ["within_24h_change"]
    assert body["appointment"]["status"] == "pending_approval"
    assert client.get(f"/appointments/{old['id']}", headers=patient_headers).json()["status"] == (
        "confirmed"
    )
    assert _active_holds(db, old_slots) == 1 and _active_holds(db, new_slots) == 1


def test_reschedule_to_taken_slot_409_old_unchanged(
    client: TestClient, db: Session, patient_headers: Headers, login: Callable[..., Headers]
) -> None:
    taken = slot_ids_at(db, "D5", local(13, 11))
    booked(client, login(patient_email("P011")), pid("P011"), "D5", taken)
    old_slots = slot_ids_at(db, "D5", FRI_10)
    old = booked(client, patient_headers, pid("P010"), "D5", old_slots)["appointment"]
    r = client.post(
        f"/appointments/{old['id']}/reschedule", json={"slot_ids": taken}, headers=patient_headers
    )
    assert r.status_code == 409
    again = client.get(f"/appointments/{old['id']}", headers=patient_headers).json()
    assert again["status"] == "confirmed" and _active_holds(db, old_slots) == 1


def test_reschedule_to_other_doctor_slot_422(
    client: TestClient, db: Session, patient_headers: Headers
) -> None:
    old = booked(client, patient_headers, pid("P010"), "D5", slot_ids_at(db, "D5", FRI_10))
    r = client.post(
        f"/appointments/{old['appointment']['id']}/reschedule",
        json={"slot_ids": slot_ids_at(db, "D1", local(13, 11))},
        headers=patient_headers,
    )
    assert r.status_code == 422 and r.json()["error"]["code"] == "slot_doctor_mismatch"


def test_reschedule_idempotent_replay(
    client: TestClient, db: Session, patient_headers: Headers
) -> None:
    old = booked(client, patient_headers, pid("P010"), "D5", slot_ids_at(db, "D5", FRI_10))
    url = f"/appointments/{old['appointment']['id']}/reschedule"
    body = {"slot_ids": slot_ids_at(db, "D5", local(13, 11))}
    h = {**patient_headers, "Idempotency-Key": "resched-key-1"}
    first = client.post(url, json=body, headers=h)
    second = client.post(url, json=body, headers=h)
    assert first.status_code == second.status_code == 200
    assert second.headers.get("idempotent-replayed") == "true"
    assert first.json() == second.json()


# -- visit day --------------------------------------------------------------------------------


def test_check_in_and_complete_flow(
    client: TestClient,
    db: Session,
    frontdesk_headers: Headers,
    doctor_headers: Callable[[str], Headers],
) -> None:
    appt = booked(
        client, frontdesk_headers, pid("P010"), "D5", slot_ids_at(db, "D5", local(10, 10))
    )
    aid = appt["appointment"]["id"]
    r = client.post(f"/appointments/{aid}/check-in", headers=frontdesk_headers)
    assert r.status_code == 200 and r.json()["status"] == "checked_in"
    # another doctor cannot see / complete it
    assert (
        client.post(f"/appointments/{aid}/complete", headers=doctor_headers("D1")).status_code
        == 404
    )
    r = client.post(f"/appointments/{aid}/complete", headers=doctor_headers("D5"))
    assert r.status_code == 200 and r.json()["status"] == "completed"
    assert_audited(db, "appointment.check_in", aid)
    assert_audited(db, "appointment.complete", aid)


def test_check_in_wrong_day_409(
    client: TestClient, db: Session, frontdesk_headers: Headers
) -> None:
    appt = booked(client, frontdesk_headers, pid("P010"), "D5", slot_ids_at(db, "D5", FRI_10))
    r = client.post(
        f"/appointments/{appt['appointment']['id']}/check-in", headers=frontdesk_headers
    )
    assert r.status_code == 409 and r.json()["error"]["code"] == "check_in_window"


def test_check_in_too_late_409(
    client: TestClient, db: Session, frontdesk_headers: Headers, clock: MutableClock
) -> None:
    appt = booked(
        client, frontdesk_headers, pid("P010"), "D5", slot_ids_at(db, "D5", local(10, 10))
    )
    clock.set(local(10, 11, 20))
    r = client.post(
        f"/appointments/{appt['appointment']['id']}/check-in", headers=frontdesk_headers
    )
    assert r.status_code == 409


def test_complete_requires_check_in_409(
    client: TestClient,
    db: Session,
    frontdesk_headers: Headers,
    doctor_headers: Callable[[str], Headers],
) -> None:
    appt = booked(
        client, frontdesk_headers, pid("P010"), "D5", slot_ids_at(db, "D5", local(10, 10))
    )
    r = client.post(
        f"/appointments/{appt['appointment']['id']}/complete", headers=doctor_headers("D5")
    )
    assert r.status_code == 409


def test_patient_cannot_check_in_or_complete(
    client: TestClient, db: Session, patient_headers: Headers
) -> None:
    appt = booked(client, patient_headers, pid("P010"), "D5", slot_ids_at(db, "D5", local(10, 10)))
    aid = appt["appointment"]["id"]
    assert client.post(f"/appointments/{aid}/check-in", headers=patient_headers).status_code == 403
    assert client.post(f"/appointments/{aid}/complete", headers=patient_headers).status_code == 403


def test_no_show_too_early_then_flags_at_three(
    client: TestClient, db: Session, frontdesk_headers: Headers, clock: MutableClock
) -> None:
    p = db.get(Patient, pid("P010"))
    assert p is not None
    p.no_show_count = 2
    db.commit()
    appt = booked(
        client, frontdesk_headers, pid("P010"), "D5", slot_ids_at(db, "D5", local(10, 10))
    )
    aid = appt["appointment"]["id"]
    early = client.post(f"/appointments/{aid}/no-show", headers=frontdesk_headers)
    assert early.status_code == 409 and early.json()["error"]["code"] == "too_early"
    clock.set(local(10, 10, 20))
    r = client.post(f"/appointments/{aid}/no-show", headers=frontdesk_headers)
    assert r.status_code == 200 and r.json()["status"] == "no_show"
    fresh(db)
    p = db.get(Patient, pid("P010"))
    assert p is not None and p.no_show_count == 3 and p.flagged
    assert_audited(db, "patient.flagged", pid("P010"))
    assert _outbox(db, pid("P010"), "no_show_recorded") == 1


# -- reads & scoping -------------------------------------------------------------------------


def test_list_scoped_by_role(
    client: TestClient,
    db: Session,
    patient_headers: Headers,
    frontdesk_headers: Headers,
    login: Callable[..., Headers],
    doctor_headers: Callable[[str], Headers],
) -> None:
    mine = booked(client, patient_headers, pid("P010"), "D5", slot_ids_at(db, "D5", FRI_10))
    other_h = login(patient_email("P011"))
    other = booked(client, other_h, pid("P011"), "D1", slot_ids_at(db, "D1", FRI_10))

    def ids(headers: Headers, **params: str) -> set[str]:
        r = client.get("/appointments", params=params, headers=headers)
        assert r.status_code == 200, r.text
        return {a["id"] for a in r.json()["items"]}

    mine_id, other_id = mine["appointment"]["id"], other["appointment"]["id"]
    assert ids(patient_headers) == {mine_id}
    assert ids(frontdesk_headers) == {mine_id, other_id}
    assert ids(frontdesk_headers, doctor_id=did("D1")) == {other_id}
    assert ids(doctor_headers("D5")) == {mine_id}
    assert ids(frontdesk_headers, status="cancelled") == set()
    assert ids(frontdesk_headers, from_date="2026-11-14") == set()
    assert ids(frontdesk_headers, to_date="2026-11-13") == {mine_id, other_id}
    # someone else's appointment is invisible, not forbidden
    assert client.get(f"/appointments/{other_id}", headers=patient_headers).status_code == 404
    assert client.get(f"/appointments/{mine_id}", headers=doctor_headers("D1")).status_code == 404
    r = client.get("/appointments", params={"patient_id": pid("P011")}, headers=patient_headers)
    assert r.status_code == 404


def test_guardian_sees_dependant_appointments(
    client: TestClient, db: Session, login: Callable[..., Headers], frontdesk_headers: Headers
) -> None:
    appt = booked(
        client, frontdesk_headers, pid("P000"), "D4", slot_ids_at(db, "D4", local(11, 10))
    )
    h = login(guardian_email("P000"))
    r = client.get("/appointments", headers=h)
    assert [a["id"] for a in r.json()["items"]] == [appt["appointment"]["id"]]


def test_unknown_appointment_404(client: TestClient, frontdesk_headers: Headers) -> None:
    assert client.get("/appointments/nope", headers=frontdesk_headers).status_code == 404


def test_frontdesk_login_constant() -> None:
    assert FRONTDESK.startswith("frontdesk1")
