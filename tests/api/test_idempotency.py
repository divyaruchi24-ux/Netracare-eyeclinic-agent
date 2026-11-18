from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta

from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from netracare.db.models import Appointment
from tests.fixtures_api import (
    Headers,
    book_api,
    fresh,
    guardian_email,
    local,
    patient_email,
    pid,
    slot_ids_at,
)
from tests.helpers import MutableClock

FRI_10 = local(13, 10)


def _count(db: Session) -> int:
    return int(fresh(db).scalar(select(func.count()).select_from(Appointment)) or 0)


def test_replay_returns_same_body_and_header(
    client: TestClient, db: Session, patient_headers: Headers
) -> None:
    slots = slot_ids_at(db, "D5", FRI_10)
    first = book_api(client, patient_headers, pid("P010"), "D5", slots, key="same-key-001")
    second = book_api(client, patient_headers, pid("P010"), "D5", slots, key="same-key-001")
    assert first.status_code == second.status_code == 201
    assert "idempotent-replayed" not in first.headers
    assert second.headers["idempotent-replayed"] == "true"
    assert first.json() == second.json()
    assert _count(db) == 1


def test_same_key_different_body_422(
    client: TestClient, db: Session, patient_headers: Headers
) -> None:
    book_api(
        client, patient_headers, pid("P010"), "D5", slot_ids_at(db, "D5", FRI_10), key="k-reuse-01"
    )
    r = book_api(
        client,
        patient_headers,
        pid("P010"),
        "D5",
        slot_ids_at(db, "D5", local(13, 11)),
        key="k-reuse-01",
    )
    assert r.status_code == 422 and r.json()["error"]["code"] == "idempotency_key_reuse"
    assert _count(db) == 1


def test_keys_are_per_user(
    client: TestClient, db: Session, patient_headers: Headers, login: Callable[..., Headers]
) -> None:
    a = book_api(
        client, patient_headers, pid("P010"), "D5", slot_ids_at(db, "D5", FRI_10), key="shared-k1"
    )
    other = login(patient_email("P011"))
    b = book_api(
        client, other, pid("P011"), "D5", slot_ids_at(db, "D5", local(13, 11)), key="shared-k1"
    )
    assert a.status_code == b.status_code == 201
    assert "idempotent-replayed" not in b.headers
    assert _count(db) == 2


def test_expired_key_reusable(
    client: TestClient, db: Session, patient_headers: Headers, clock: MutableClock
) -> None:
    first = book_api(
        client, patient_headers, pid("P010"), "D5", slot_ids_at(db, "D5", FRI_10), key="old-key-01"
    )
    assert first.status_code == 201
    clock.advance(timedelta(hours=25))
    r = book_api(
        client,
        patient_headers,
        pid("P010"),
        "D5",
        slot_ids_at(db, "D5", local(14, 10)),
        key="old-key-01",
    )
    assert r.status_code == 201, r.text
    assert "idempotent-replayed" not in r.headers


def test_invalid_key_format_422(client: TestClient, db: Session, patient_headers: Headers) -> None:
    r = book_api(
        client, patient_headers, pid("P010"), "D5", slot_ids_at(db, "D5", FRI_10), key="bad key!"
    )
    assert r.status_code == 422 and r.json()["error"]["code"] == "invalid_idempotency_key"


def test_error_responses_are_not_stored(
    client: TestClient, db: Session, login: Callable[..., Headers]
) -> None:
    """A failed attempt (e.g. 409) can be retried with the same key once fixed."""
    h = login(guardian_email("P001"))
    slots = slot_ids_at(db, "D4", local(11, 10))
    wrong = book_api(client, h, pid("P001"), "D4", slots, key="retry-key-1", type="new_patient")
    assert wrong.status_code == 422
    ok = book_api(client, h, pid("P001"), "D4", slots, key="retry-key-1")
    assert ok.status_code == 201


def test_purge_expired_releases_keys(
    client: TestClient, db: Session, patient_headers: Headers, clock: MutableClock
) -> None:
    from netracare.services.idempotency_service import IdempotencyService

    book_api(
        client, patient_headers, pid("P010"), "D5", slot_ids_at(db, "D5", FRI_10), key="purge-k-01"
    )
    clock.advance(timedelta(hours=25))
    fresh(db)
    assert IdempotencyService(db, clock).purge_expired() == 1
    db.commit()
    appt = db.scalar(select(Appointment))
    assert appt is not None and appt.idempotency_key is None
