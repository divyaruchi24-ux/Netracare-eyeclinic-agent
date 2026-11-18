from __future__ import annotations

from collections.abc import Callable

from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from netracare.db.models import NotificationOutbox, Slot
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


def _open_slots(db: Session, code: str, day: int, month: int = 11) -> int:
    start, end = local(day, 0, month=month), local(day, 23, 59, month=month)
    return int(
        fresh(db).scalar(
            select(func.count())
            .select_from(Slot)
            .where(
                Slot.doctor_id == did(code),
                Slot.status == SlotStatus.OPEN,
                Slot.start_utc >= start,
                Slot.start_utc < end,
            )
        )
        or 0
    )


def _leave(client: TestClient, headers: Headers, code: str, start: str, end: str) -> object:
    return client.post(
        f"/doctors/{did(code)}/leaves",
        json={"start": start, "end": end, "reason": "Personal"},
        headers=headers,
    )


def test_add_leave_blocks_slots_and_reports_affected(
    client: TestClient, db: Session, patient_headers: Headers, admin_headers: Headers
) -> None:
    appt = booked(client, patient_headers, pid("P010"), "D2", slot_ids_at(db, "D2", local(13, 10)))
    r = _leave(
        client, admin_headers, "D2", "2026-11-13T09:00:00+05:30", "2026-11-13T13:00:00+05:30"
    )
    assert r.status_code == 201, r.text  # type: ignore[attr-defined]
    body = r.json()  # type: ignore[attr-defined]
    assert body["blocked_slots"] == 16
    assert [a["id"] for a in body["affected_appointments"]] == [appt["appointment"]["id"]]
    assert _open_slots(db, "D2", 13) == 0
    n = fresh(db).scalar(
        select(func.count())
        .select_from(NotificationOutbox)
        .where(NotificationOutbox.template == "leave_conflict")
    )
    assert n == 1
    assert_audited(db, "schedule.leave_add", body["leave"]["id"])
    # the booking itself is untouched - the front desk decides what to do
    got = client.get(f"/appointments/{appt['appointment']['id']}", headers=patient_headers)
    assert got.json()["status"] == "confirmed"


def test_remove_leave_reopens(client: TestClient, db: Session, admin_headers: Headers) -> None:
    r = _leave(
        client, admin_headers, "D2", "2026-11-13T09:00:00+05:30", "2026-11-13T13:00:00+05:30"
    )
    leave_id = r.json()["leave"]["id"]  # type: ignore[attr-defined]
    listed = client.get(f"/doctors/{did('D2')}/leaves", headers=admin_headers).json()
    assert leave_id in {lv["id"] for lv in listed}
    out = client.delete(f"/leaves/{leave_id}", headers=admin_headers)
    assert out.status_code == 200 and out.json()["count"] == 16
    assert _open_slots(db, "D2", 13) == 16


def test_remove_seeded_leave_generates_missing_slots(
    client: TestClient, db: Session, admin_headers: Headers
) -> None:
    # D2's seeded conference leave covers Nov 20-22, so no slots were ever generated.
    assert _open_slots(db, "D2", 20) == 0
    leaves = client.get(f"/doctors/{did('D2')}/leaves", headers=admin_headers).json()
    client.delete(f"/leaves/{leaves[0]['id']}", headers=admin_headers)
    assert _open_slots(db, "D2", 20) == 16  # Fri 09:00-13:00


def test_remove_leave_keeps_slots_blocked_by_holiday(
    client: TestClient, db: Session, admin_headers: Headers
) -> None:
    r = _leave(
        client, admin_headers, "D5", "2026-11-13T09:00:00+05:30", "2026-11-13T18:00:00+05:30"
    )
    client.post(
        "/holidays", json={"day": "2026-11-13", "name": "Unexpected closure"}, headers=admin_headers
    )
    client.delete(f"/leaves/{r.json()['leave']['id']}", headers=admin_headers)  # type: ignore[attr-defined]
    assert _open_slots(db, "D5", 13) == 0


def test_leave_validation(client: TestClient, admin_headers: Headers) -> None:
    bad = _leave(
        client, admin_headers, "D2", "2026-11-13T13:00:00+05:30", "2026-11-13T09:00:00+05:30"
    )
    assert bad.status_code == 422  # type: ignore[attr-defined]
    naive = _leave(client, admin_headers, "D2", "2026-11-13T09:00:00", "2026-11-13T10:00:00")
    assert naive.status_code == 422  # type: ignore[attr-defined]
    past = _leave(
        client, admin_headers, "D2", "2026-11-01T09:00:00+05:30", "2026-11-02T10:00:00+05:30"
    )
    assert past.json()["error"]["code"] == "leave_in_past"  # type: ignore[attr-defined]
    long = _leave(
        client, admin_headers, "D2", "2026-12-01T09:00:00+05:30", "2027-04-01T10:00:00+05:30"
    )
    assert long.json()["error"]["code"] == "leave_too_long"  # type: ignore[attr-defined]
    ok = _leave(
        client, admin_headers, "D2", "2026-11-13T09:00:00+05:30", "2026-11-13T10:00:00+05:30"
    )
    assert ok.status_code == 201  # type: ignore[attr-defined]
    dup = _leave(
        client, admin_headers, "D2", "2026-11-13T09:30:00+05:30", "2026-11-13T11:00:00+05:30"
    )
    assert dup.status_code == 409  # type: ignore[attr-defined]
    missing = client.post(
        "/doctors/nope/leaves",
        json={"start": "2026-11-13T09:00:00+05:30", "end": "2026-11-13T10:00:00+05:30"},
        headers=admin_headers,
    )
    assert missing.status_code == 404
    assert client.delete("/leaves/nope", headers=admin_headers).status_code == 404


def test_holiday_blocks_all_doctors_and_notifies(
    client: TestClient, db: Session, patient_headers: Headers, admin_headers: Headers
) -> None:
    appt = booked(client, patient_headers, pid("P010"), "D5", slot_ids_at(db, "D5", local(13, 10)))
    r = client.post(
        "/holidays", json={"day": "2026-11-13", "name": "Closure"}, headers=admin_headers
    )
    assert r.status_code == 201
    body = r.json()
    assert body["blocked_slots"] > 50
    assert [a["id"] for a in body["affected_appointments"]] == [appt["appointment"]["id"]]
    assert all(_open_slots(db, c, 13) == 0 for c in ("D1", "D2", "D3", "D5"))
    assert any(
        h["day"] == "2026-11-13" for h in client.get("/holidays", headers=admin_headers).json()
    )
    dup = client.post("/holidays", json={"day": "2026-11-13", "name": "x"}, headers=admin_headers)
    assert dup.status_code == 409
    past = client.post("/holidays", json={"day": "2026-11-01", "name": "x"}, headers=admin_headers)
    assert past.status_code == 422
    # removing it reopens slots, but D2's Fri 20th leave is a different day so it is unaffected
    out = client.delete("/holidays/2026-11-13", headers=admin_headers)
    assert out.json()["count"] == body["blocked_slots"]
    assert _open_slots(db, "D5", 13) > 0
    assert client.delete("/holidays/2026-11-13", headers=admin_headers).status_code == 404
    assert_audited(db, "schedule.holiday_remove")


def test_add_template_generates_slots_and_remove_blocks_them(
    client: TestClient, db: Session, admin_headers: Headers
) -> None:
    assert _open_slots(db, "D1", 11) == 0  # Dr. Rao has no Wednesday OPD
    r = client.post(
        f"/doctors/{did('D1')}/schedule-templates",
        json={"weekday": 2, "start_local": "09:00", "end_local": "12:00"},
        headers=admin_headers,
    )
    assert r.status_code == 201, r.text
    assert r.json()["opened_slots"] > 12 * 7
    assert _open_slots(db, "D1", 11) == 12
    tpls = client.get(f"/doctors/{did('D1')}/schedule-templates", headers=admin_headers).json()
    new = next(t for t in tpls if t["weekday"] == 2)
    out = client.delete(f"/schedule-templates/{new['id']}", headers=admin_headers)
    assert out.status_code == 200 and out.json()["blocked_slots"] > 12 * 7
    assert _open_slots(db, "D1", 11) == 0
    assert _open_slots(db, "D1", 13) > 0  # Friday untouched


def test_remove_template_reports_booked_appointments(
    client: TestClient,
    db: Session,
    patient_headers: Headers,
    admin_headers: Headers,
) -> None:
    appt = booked(client, patient_headers, pid("P010"), "D5", slot_ids_at(db, "D5", local(13, 10)))
    tpls = client.get(f"/doctors/{did('D5')}/schedule-templates", headers=admin_headers).json()
    friday = next(t for t in tpls if t["weekday"] == 4)
    out = client.delete(f"/schedule-templates/{friday['id']}", headers=admin_headers).json()
    assert appt["appointment"]["id"] in {a["id"] for a in out["affected_appointments"]}


def test_template_validation(client: TestClient, admin_headers: Headers) -> None:
    def add(weekday: int, start: str, end: str) -> int:
        r = client.post(
            f"/doctors/{did('D1')}/schedule-templates",
            json={"weekday": weekday, "start_local": start, "end_local": end},
            headers=admin_headers,
        )
        return r.status_code

    assert add(6, "09:00", "12:00") == 422  # Sunday
    assert add(2, "08:00", "12:00") == 422  # before opening
    assert add(2, "09:10", "12:00") == 422  # off-grid
    assert add(2, "12:00", "09:00") == 422
    assert add(0, "10:00", "11:00") == 409  # overlaps Monday block
    assert client.delete("/schedule-templates/nope", headers=admin_headers).status_code == 404
    assert client.get("/doctors/nope/schedule-templates", headers=admin_headers).status_code == 404


def test_regenerate_idempotent(client: TestClient, admin_headers: Headers, db: Session) -> None:
    first = client.post("/admin/slots/regenerate", json={}, headers=admin_headers)
    second = client.post("/admin/slots/regenerate", json={"weeks": 8}, headers=admin_headers)
    assert first.status_code == second.status_code == 200
    assert second.json()["count"] == 0
    longer = client.post(
        "/admin/slots/regenerate", json={"doctor_id": did("D5"), "weeks": 9}, headers=admin_headers
    )
    assert longer.json()["count"] > 0
    assert_audited(db, "schedule.regenerate")


def test_non_admin_forbidden(
    client: TestClient,
    frontdesk_headers: Headers,
    patient_headers: Headers,
    doctor_headers: Callable[[str], Headers],
) -> None:
    for h in (frontdesk_headers, patient_headers, doctor_headers("D2")):
        r = _leave(client, h, "D2", "2026-11-13T09:00:00+05:30", "2026-11-13T10:00:00+05:30")
        assert r.status_code == 403  # type: ignore[attr-defined]
        assert (
            client.post("/holidays", json={"day": "2026-12-01", "name": "x"}, headers=h).status_code
            == 403
        )
