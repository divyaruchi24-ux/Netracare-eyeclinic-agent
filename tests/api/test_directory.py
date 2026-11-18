from __future__ import annotations

from collections.abc import Callable

from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from netracare.db.models import Slot
from netracare.domain.enums import SlotStatus
from tests.fixtures_api import (
    Headers,
    assert_audited,
    booked,
    did,
    fresh,
    guardian_email,
    local,
    patient_email,
    pid,
    slot_ids_at,
)


def test_public_directory_reads(client: TestClient) -> None:
    specs = client.get("/specialties").json()
    assert {s["code"] for s in specs} >= {"CORNEA", "RETINA", "PEDIATRIC"}
    docs = client.get("/doctors").json()
    assert [d["code"] for d in docs] == ["D1", "D2", "D3", "D4", "D5"]
    retina = client.get("/doctors", params={"specialty": "RETINA"}).json()
    assert [d["code"] for d in retina] == ["D2"]
    one = client.get(f"/doctors/{did('D4')}").json()
    assert one["specialty"]["code"] == "PEDIATRIC" and one["bio_slug"]
    assert client.get("/doctors/nope").status_code == 404


def test_admin_creates_doctor(client: TestClient, db: Session, admin_headers: Headers) -> None:
    body = {"code": "D6", "full_name": "Dr. Test Six", "specialty_code": "GENERAL"}
    r = client.post("/doctors", json=body, headers=admin_headers)
    assert r.status_code == 201, r.text
    assert_audited(db, "doctor.create", r.json()["id"])
    assert client.post("/doctors", json=body, headers=admin_headers).status_code == 409
    bad = client.post(
        "/doctors", json={**body, "code": "D7", "specialty_code": "X"}, headers=admin_headers
    )
    assert bad.status_code == 422
    lower = client.post("/doctors", json={**body, "code": "d8"}, headers=admin_headers)
    assert lower.status_code == 422


def test_non_admin_cannot_create_doctor(
    client: TestClient, frontdesk_headers: Headers, patient_headers: Headers
) -> None:
    body = {"code": "D6", "full_name": "Dr. X", "specialty_code": "GENERAL"}
    assert client.post("/doctors", json=body).status_code == 401
    assert client.post("/doctors", json=body, headers=frontdesk_headers).status_code == 403
    assert client.post("/doctors", json=body, headers=patient_headers).status_code == 403


def test_update_doctor(client: TestClient, db: Session, admin_headers: Headers) -> None:
    r = client.patch(
        f"/doctors/{did('D3')}",
        json={"full_name": "Dr. Meera Iyer-Rao", "specialty_code": "GENERAL"},
        headers=admin_headers,
    )
    assert r.status_code == 200
    assert r.json()["doctor"]["full_name"] == "Dr. Meera Iyer-Rao"
    assert r.json()["doctor"]["specialty"]["code"] == "GENERAL"
    row = assert_audited(db, "doctor.update", did("D3"))
    assert row.before_json and row.before_json["full_name"] == "Dr. Meera Iyer"


def test_deactivate_with_future_bookings_needs_force(
    client: TestClient, db: Session, admin_headers: Headers, patient_headers: Headers
) -> None:
    appt = booked(client, patient_headers, pid("P010"), "D5", slot_ids_at(db, "D5", local(13, 10)))
    r = client.patch(f"/doctors/{did('D5')}", json={"is_active": False}, headers=admin_headers)
    assert r.status_code == 409 and r.json()["error"]["code"] == "doctor_has_appointments"
    forced = client.patch(
        f"/doctors/{did('D5')}",
        params={"force": "true"},
        json={"is_active": False},
        headers=admin_headers,
    )
    assert forced.status_code == 200
    assert forced.json()["affected_appointment_ids"] == [appt["appointment"]["id"]]
    open_left = fresh(db).scalar(
        select(func.count())
        .select_from(Slot)
        .where(
            Slot.doctor_id == did("D5"),
            Slot.status == SlotStatus.OPEN,
            Slot.start_utc > local(10, 9),
        )
    )
    assert open_left == 0
    assert [d["code"] for d in client.get("/doctors").json()] == ["D1", "D2", "D3", "D4"]
    assert "D5" in [d["code"] for d in client.get("/doctors", params={"active": "false"}).json()]


def test_patient_search_staff_only(
    client: TestClient, frontdesk_headers: Headers, patient_headers: Headers
) -> None:
    r = client.get("/patients", params={"q": "example.com", "limit": 5}, headers=frontdesk_headers)
    body = r.json()
    assert r.status_code == 200 and body["total"] >= 20 and len(body["items"]) == 5
    item = body["items"][0]
    assert (
        item["phone_masked"].endswith(item["phone_masked"][-4:])
        and "+91" not in item["phone_masked"]
    )
    assert client.get("/patients", headers=patient_headers).status_code == 403
    phone = client.get("/patients", params={"q": "90000 00010"}, headers=frontdesk_headers).json()
    assert [p["id"] for p in phone["items"]] == [pid("P010")]


def test_get_patient_scoped(
    client: TestClient,
    db: Session,
    patient_headers: Headers,
    login: Callable[..., Headers],
    doctor_headers: Callable[[str], Headers],
    frontdesk_headers: Headers,
) -> None:
    assert client.get(f"/patients/{pid('P010')}", headers=patient_headers).status_code == 200
    assert client.get(f"/patients/{pid('P011')}", headers=patient_headers).status_code == 404
    guardian = login(guardian_email("P000"))
    minor = client.get(f"/patients/{pid('P000')}", headers=guardian).json()
    assert minor["is_minor"] is True and minor["guardians"][0]["relationship"] in {
        "mother",
        "father",
    }
    # a doctor sees a patient only once they have an appointment with them
    assert client.get(f"/patients/{pid('P010')}", headers=doctor_headers("D5")).status_code == 404
    booked(client, frontdesk_headers, pid("P010"), "D5", slot_ids_at(db, "D5", local(13, 10)))
    assert client.get(f"/patients/{pid('P010')}", headers=doctor_headers("D5")).status_code == 200


def test_link_guardian(
    client: TestClient, db: Session, frontdesk_headers: Headers, login: Callable[..., Headers]
) -> None:
    guardian = patient_email("P011")
    url = f"/patients/{pid('P001')}/guardians"
    r = client.post(
        url,
        json={"guardian_email": guardian, "relationship": "guardian"},
        headers=frontdesk_headers,
    )
    assert r.status_code == 201, r.text
    assert_audited(db, "patient.guardian_link", pid("P001"))
    me = client.get("/auth/me", headers=login(guardian)).json()
    assert set(me["patient_ids"]) == {pid("P011"), pid("P001")}
    again = client.post(
        url,
        json={"guardian_email": guardian, "relationship": "guardian"},
        headers=frontdesk_headers,
    )
    assert again.status_code == 409


def test_link_guardian_validation(
    client: TestClient, frontdesk_headers: Headers, patient_headers: Headers
) -> None:
    url = f"/patients/{pid('P001')}/guardians"

    def post(email: str, rel: str, headers: Headers = frontdesk_headers) -> tuple[int, str]:
        r = client.post(url, json={"guardian_email": email, "relationship": rel}, headers=headers)
        return r.status_code, r.json().get("error", {}).get("code", "")

    assert post(patient_email("P011"), "uncle-ish") == (422, "invalid_relationship")
    assert post("admin@netracare.example", "guardian")[0] == 422
    assert post("ghost@example.com", "guardian") == (422, "guardian_not_found")
    assert post(patient_email("P011"), "guardian", patient_headers)[0] == 403
    self_link = client.post(
        f"/patients/{pid('P011')}/guardians",
        json={"guardian_email": patient_email("P011"), "relationship": "other"},
        headers=frontdesk_headers,
    )
    assert self_link.json()["error"]["code"] == "self_guardian"
    missing = client.post(
        "/patients/nope/guardians",
        json={"guardian_email": patient_email("P011"), "relationship": "other"},
        headers=frontdesk_headers,
    )
    assert missing.status_code == 404
