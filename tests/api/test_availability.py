from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from tests.fixtures_api import Headers, booked, did, local, pid, slot_ids_at


def _options(client: TestClient, headers: Headers, **params: str) -> list[dict[str, object]]:
    r = client.get("/availability", params=params, headers=headers)
    assert r.status_code == 200, r.text
    return [o for d in r.json()["days"] for o in d["options"]]


def _starts(opts: list[dict[str, object]]) -> set[str]:
    return {str(o["start_local"])[11:16] for o in opts}


def test_routine_options_for_a_day(client: TestClient, patient_headers: Headers) -> None:
    opts = _options(
        client, patient_headers, doctor_id=did("D5"), from_date="2026-11-13", to_date="2026-11-13"
    )
    starts = _starts(opts)
    assert len(opts) == 32  # 09:00-18:00 minus lunch, 15-min slots
    assert "13:00" not in starts and "13:45" not in starts and "17:45" in starts


def test_thirty_minute_pairs_do_not_span_lunch(
    client: TestClient, patient_headers: Headers
) -> None:
    opts = _options(
        client,
        patient_headers,
        doctor_id=did("D5"),
        from_date="2026-11-13",
        to_date="2026-11-13",
        type="new_patient",
    )
    starts = _starts(opts)
    assert "12:30" in starts and "12:45" not in starts and "17:45" not in starts
    assert all(len(o["slot_ids"]) == 2 for o in opts)  # type: ignore[arg-type]


def test_past_slots_excluded_today(client: TestClient, patient_headers: Headers) -> None:
    starts = _starts(
        _options(
            client,
            patient_headers,
            doctor_id=did("D5"),
            from_date="2026-11-10",
            to_date="2026-11-10",
        )
    )
    assert "09:00" not in starts and "09:15" in starts


def test_from_date_in_past_is_clamped(client: TestClient, patient_headers: Headers) -> None:
    opts = _options(
        client, patient_headers, doctor_id=did("D5"), from_date="2026-11-01", to_date="2026-11-10"
    )
    assert opts and all(str(o["start_local"]).startswith("2026-11-10") for o in opts)


def test_held_slot_excluded(client: TestClient, db: Session, patient_headers: Headers) -> None:
    booked(client, patient_headers, pid("P010"), "D5", slot_ids_at(db, "D5", local(13, 10)))
    starts = _starts(
        _options(
            client,
            patient_headers,
            doctor_id=did("D5"),
            from_date="2026-11-13",
            to_date="2026-11-13",
        )
    )
    assert "10:00" not in starts and "10:15" in starts


def test_leave_excluded(client: TestClient, patient_headers: Headers) -> None:
    opts = _options(
        client, patient_headers, doctor_id=did("D2"), from_date="2026-11-20", to_date="2026-11-22"
    )
    assert opts == []


def test_by_specialty(client: TestClient, patient_headers: Headers) -> None:
    opts = _options(
        client, patient_headers, specialty="RETINA", from_date="2026-11-13", to_date="2026-11-13"
    )
    assert opts and {o["doctor_id"] for o in opts} == {did("D2")}


def test_default_range_is_a_week(client: TestClient, patient_headers: Headers) -> None:
    r = client.get("/availability", params={"doctor_id": did("D5")}, headers=patient_headers)
    days = [d["date"] for d in r.json()["days"]]
    assert days[0] == "2026-11-10" and days[-1] <= "2026-11-16"


def test_validation_errors(client: TestClient, patient_headers: Headers) -> None:
    def get(**p: str) -> tuple[int, str]:
        r = client.get("/availability", params=p, headers=patient_headers)
        return r.status_code, r.json().get("error", {}).get("code", "")

    assert get(from_date="2026-11-13") == (422, "doctor_or_specialty_required")
    assert get(doctor_id=did("D5"), from_date="2026-11-13", to_date="2026-12-31")[0] == 422
    assert get(doctor_id=did("D5"), from_date="2026-11-13", to_date="2026-11-12")[0] == 422
    assert get(doctor_id="nope")[0] == 404
    assert get(specialty="NOPE")[0] == 404
    assert get(doctor_id=did("D5"), type="weird")[0] == 422


def test_requires_auth(client: TestClient) -> None:
    assert client.get("/availability", params={"doctor_id": did("D5")}).status_code == 401
