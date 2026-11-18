from __future__ import annotations

from collections.abc import Callable

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from netracare.services.notification_service import TEMPLATES, NotificationService
from tests.fixtures_api import (
    IST,
    Headers,
    booked,
    guardian_email,
    local,
    patient_email,
    pid,
    slot_ids_at,
)


def test_patient_sees_only_own_notifications(
    client: TestClient,
    db: Session,
    patient_headers: Headers,
    frontdesk_headers: Headers,
    login: Callable[..., Headers],
) -> None:
    booked(client, patient_headers, pid("P010"), "D5", slot_ids_at(db, "D5", local(13, 10)))
    other = login(patient_email("P011"))
    booked(client, other, pid("P011"), "D5", slot_ids_at(db, "D5", local(13, 11)))
    mine = client.get("/notifications", headers=patient_headers).json()
    assert mine["total"] == 1 and mine["items"][0]["to_patient_id"] == pid("P010")
    body = mine["items"][0]["payload_json"]["body"]
    assert "confirmed" in body and "Fri 13 Nov 2026, 10:00 AM" in body
    assert mine["items"][0]["channel"] == "sms"
    staff = client.get("/notifications", headers=frontdesk_headers).json()
    assert staff["total"] == 2
    filtered = client.get(
        "/notifications", params={"patient_id": pid("P011")}, headers=frontdesk_headers
    ).json()
    assert filtered["total"] == 1
    snoop = client.get(
        "/notifications", params={"patient_id": pid("P011")}, headers=patient_headers
    )
    assert snoop.status_code == 404


def test_guardian_sees_dependant_notifications(
    client: TestClient, db: Session, login: Callable[..., Headers], frontdesk_headers: Headers
) -> None:
    booked(client, frontdesk_headers, pid("P000"), "D4", slot_ids_at(db, "D4", local(11, 10)))
    g = login(guardian_email("P000"))
    assert client.get("/notifications", headers=g).json()["total"] == 1


def test_doctor_forbidden(client: TestClient, doctor_headers: Callable[[str], Headers]) -> None:
    assert client.get("/notifications", headers=doctor_headers("D1")).status_code == 403


def test_unknown_template_rejected(db: Session) -> None:
    with pytest.raises(ValueError, match="unknown notification template"):
        NotificationService(db, IST).enqueue(pid("P010"), "nope", {})


def test_templates_never_mention_clinical_advice() -> None:
    banned = ("drop", "dose", "medicine", "medication", "diagnos")
    assert not [t for t in TEMPLATES.values() if any(b in t.lower() for b in banned)]
