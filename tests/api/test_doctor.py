from __future__ import annotations

from collections.abc import Callable

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from tests.fixtures_api import Headers, booked, local, pid, slot_ids_at


def test_doctor_sees_own_schedule_with_bookings(
    client: TestClient,
    db: Session,
    frontdesk_headers: Headers,
    doctor_headers: Callable[[str], Headers],
) -> None:
    appt = booked(
        client,
        frontdesk_headers,
        pid("P010"),
        "D5",
        slot_ids_at(db, "D5", local(13, 10)),
        reason="itchy eyes",
    )
    r = client.get(
        "/doctors/me/schedule",
        params={"from_date": "2026-11-13", "to_date": "2026-11-13"},
        headers=doctor_headers("D5"),
    )
    assert r.status_code == 200
    (day,) = r.json()
    assert day["date"] == "2026-11-13" and day["open_slots"] == 31
    entry = next(e for e in day["entries"] if e["appointment_id"])
    assert entry["appointment_id"] == appt["appointment"]["id"]
    assert (
        entry["patient_name"] and entry["reason"] == "itchy eyes" and entry["status"] == "confirmed"
    )
    other = client.get(
        "/doctors/me/schedule",
        params={"from_date": "2026-11-13", "to_date": "2026-11-13"},
        headers=doctor_headers("D1"),
    ).json()
    assert all(e["appointment_id"] is None for d in other for e in d["entries"])


def test_default_range_and_validation(
    client: TestClient, doctor_headers: Callable[[str], Headers]
) -> None:
    h = doctor_headers("D5")
    days = client.get("/doctors/me/schedule", headers=h).json()
    assert days[0]["date"] == "2026-11-10" and len(days) <= 7
    too_long = client.get(
        "/doctors/me/schedule",
        params={"from_date": "2026-11-10", "to_date": "2026-12-31"},
        headers=h,
    )
    assert too_long.status_code == 422


def test_non_doctor_forbidden(
    client: TestClient, frontdesk_headers: Headers, patient_headers: Headers
) -> None:
    assert client.get("/doctors/me/schedule", headers=frontdesk_headers).status_code == 403
    assert client.get("/doctors/me/schedule", headers=patient_headers).status_code == 403
    assert client.get("/doctors/me/schedule").status_code == 401
