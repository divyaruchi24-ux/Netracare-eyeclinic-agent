from datetime import timedelta

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from netracare.db.models import Appointment, Slot
from netracare.repositories.appointment_repo import AppointmentRepository
from tests.db.factories import T0, make_appointment, make_doctor, make_patient, make_slot

pytestmark = pytest.mark.db


def test_two_active_holds_on_same_slot_rejected(session: Session) -> None:
    doc = make_doctor(session)
    slot = make_slot(session, doc)
    repo = AppointmentRepository(session)
    a1 = make_appointment(session, make_patient(session, "A"), doc, slot)
    a2 = make_appointment(session, make_patient(session, "B"), doc, slot)
    repo.hold_slots(a1, [slot.id])
    with pytest.raises(IntegrityError):
        repo.hold_slots(a2, [slot.id])


def test_inactive_hold_allows_rebook(session: Session) -> None:
    doc = make_doctor(session)
    slot = make_slot(session, doc)
    repo = AppointmentRepository(session)
    a1 = make_appointment(session, make_patient(session, "A"), doc, slot)
    repo.hold_slots(a1, [slot.id])
    assert repo.release_slots(a1.id) == 1
    a2 = make_appointment(session, make_patient(session, "B"), doc, slot)
    repo.hold_slots(a2, [slot.id])  # no error


def test_slot_unique_per_doctor_start(session: Session) -> None:
    doc = make_doctor(session)
    make_slot(session, doc)
    session.add(Slot(doctor_id=doc.id, start_utc=T0, end_utc=T0 + timedelta(minutes=15)))
    with pytest.raises(IntegrityError):
        session.flush()


def test_idempotency_key_unique(session: Session) -> None:
    doc = make_doctor(session)
    p = make_patient(session)
    s1 = make_slot(session, doc)
    s2 = make_slot(session, doc, T0 + timedelta(minutes=15))
    a1 = make_appointment(session, p, doc, s1)
    a1.idempotency_key = "abc"
    session.flush()
    a2 = make_appointment(session, p, doc, s2)
    a2.idempotency_key = "abc"
    with pytest.raises(IntegrityError):
        session.flush()


def test_datetimes_round_trip_as_utc(session: Session) -> None:
    doc = make_doctor(session)
    slot = make_slot(session, doc)
    session.expire_all()
    loaded = session.get(Slot, slot.id)
    assert loaded is not None
    assert loaded.start_utc == T0
    assert loaded.start_utc.tzinfo is not None


def test_optimistic_version_increments(session: Session) -> None:
    doc = make_doctor(session)
    a = make_appointment(session, make_patient(session), doc, make_slot(session, doc))
    assert a.version == 1
    a.reason_text = "changed"
    session.flush()
    assert session.get(Appointment, a.id).version == 2  # type: ignore[union-attr]


def _approval(session: Session, appointment_id: str, user_id: str, status: str) -> None:
    from netracare.db.models import ApprovalRequest
    from netracare.domain.enums import ApprovalKind, ApprovalStatus

    session.add(
        ApprovalRequest(
            kind=ApprovalKind.CANCEL,
            appointment_id=appointment_id,
            requested_by_user_id=user_id,
            status=ApprovalStatus(status),
            reasons=["within_24h_change"],
        )
    )
    session.flush()


def test_one_pending_approval_per_appointment(session: Session) -> None:
    from netracare.db.models import User
    from netracare.domain.enums import Role

    doc = make_doctor(session)
    appt = make_appointment(session, make_patient(session), doc, make_slot(session, doc))
    user = User(email="u@example.com", password_hash="x", role=Role.PATIENT)
    session.add(user)
    session.flush()
    _approval(session, appt.id, user.id, "rejected")
    _approval(session, appt.id, user.id, "pending")  # a decided one doesn't block
    with pytest.raises(IntegrityError):
        _approval(session, appt.id, user.id, "pending")


def test_idempotency_key_unique_per_user(session: Session) -> None:
    from netracare.db.models import IdempotencyRecord, User
    from netracare.domain.enums import Role

    u1 = User(email="a@example.com", password_hash="x", role=Role.PATIENT)
    u2 = User(email="b@example.com", password_hash="x", role=Role.PATIENT)
    session.add_all([u1, u2])
    session.flush()

    def rec(user_id: str) -> IdempotencyRecord:
        return IdempotencyRecord(
            user_id=user_id,
            key="k-123456",
            method="POST",
            path="/appointments",
            request_hash="h",
            status_code=201,
            response_json={},
        )

    session.add_all([rec(u1.id), rec(u2.id)])
    session.flush()  # same key for different users is fine
    session.add(rec(u1.id))
    with pytest.raises(IntegrityError):
        session.flush()
