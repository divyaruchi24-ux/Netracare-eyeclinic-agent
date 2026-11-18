from __future__ import annotations

from collections.abc import Callable

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from netracare.db.models import Slot
from netracare.domain.enums import SlotStatus
from tests.fixtures_api import (
    Headers,
    assert_audited,
    booked,
    did,
    fresh,
    local,
    pid,
    slot_ids_at,
)
from tests.helpers import MutableClock


def test_queue_groups_by_doctor(
    client: TestClient, db: Session, frontdesk_headers: Headers
) -> None:
    a = booked(client, frontdesk_headers, pid("P010"), "D5", slot_ids_at(db, "D5", local(10, 10)))
    booked(client, frontdesk_headers, pid("P011"), "D5", slot_ids_at(db, "D5", local(10, 11)))
    booked(
        client, frontdesk_headers, pid("P012"), "D5", slot_ids_at(db, "D5", local(13, 11))
    )  # Fri
    q = client.get("/front-desk/queue", headers=frontdesk_headers).json()
    assert q["date"] == "2026-11-10"
    by_code = {d["doctor_name"]: d for d in q["doctors"]}
    nair = next(d for d in q["doctors"] if d["doctor_id"] == did("D5"))
    assert nair["appointments"][0]["id"] == a["appointment"]["id"]
    assert len(nair["appointments"]) == 2
    assert q["status_counts"] == {"confirmed": 2}
    assert len(by_code) == 5
    fri = client.get("/front-desk/queue", params={"date": "2026-11-13"}, headers=frontdesk_headers)
    assert fri.json()["status_counts"] == {"confirmed": 1}


def test_walk_in_existing_patient_gets_next_slot_today(
    client: TestClient, db: Session, frontdesk_headers: Headers, clock: MutableClock
) -> None:
    clock.set(local(10, 11, 5))
    r = client.post(
        "/front-desk/walk-ins",
        json={"doctor_id": did("D5"), "patient_id": pid("P006"), "reason": "red eye"},
        headers=frontdesk_headers,
    )
    assert r.status_code == 201, r.text
    appt = r.json()["appointment"]
    assert appt["start_local"].startswith("2026-11-10T11:15")
    assert appt["status"] == "confirmed"  # flagged patient, but staff walk-in overrides
    assert_audited(db, "appointment.override", appt["id"])


def test_walk_in_new_patient(client: TestClient, db: Session, frontdesk_headers: Headers) -> None:
    r = client.post(
        "/front-desk/walk-ins",
        json={
            "doctor_id": did("D5"),
            "new_patient": {
                "full_name": "Walk In Person",
                "dob": "1985-03-03",
                "phone": "+91 90000 99999",
            },
        },
        headers=frontdesk_headers,
    )
    assert r.status_code == 201, r.text
    new_pid = r.json()["appointment"]["patient"]["id"]
    assert_audited(db, "patient.create", new_pid)
    got = client.get(f"/patients/{new_pid}", headers=frontdesk_headers).json()
    assert got["has_account"] is False and got["full_name"] == "Walk In Person"


def test_walk_in_when_fully_booked_409(
    client: TestClient, db: Session, frontdesk_headers: Headers
) -> None:
    for s in fresh(db).scalars(select(Slot).where(Slot.doctor_id == did("D5"))):
        s.status = SlotStatus.BLOCKED
    db.commit()
    r = client.post(
        "/front-desk/walk-ins",
        json={"doctor_id": did("D5"), "patient_id": pid("P010")},
        headers=frontdesk_headers,
    )
    assert r.status_code == 409 and r.json()["error"]["code"] == "no_slots_today"


def test_walk_in_validation(client: TestClient, frontdesk_headers: Headers) -> None:
    both = client.post(
        "/front-desk/walk-ins",
        json={
            "doctor_id": did("D5"),
            "patient_id": pid("P010"),
            "new_patient": {"full_name": "X", "dob": "1990-01-01"},
        },
        headers=frontdesk_headers,
    )
    assert both.status_code == 422
    neither = client.post(
        "/front-desk/walk-ins", json={"doctor_id": did("D5")}, headers=frontdesk_headers
    )
    assert neither.status_code == 422
    future = client.post(
        "/front-desk/walk-ins",
        json={"doctor_id": did("D5"), "new_patient": {"full_name": "X", "dob": "2099-01-01"}},
        headers=frontdesk_headers,
    )
    assert future.status_code == 422


def test_front_desk_routes_forbidden_for_others(
    client: TestClient, patient_headers: Headers, doctor_headers: Callable[[str], Headers]
) -> None:
    for h in (patient_headers, doctor_headers("D5")):
        assert client.get("/front-desk/queue", headers=h).status_code == 403
