"""Service-level tests for the last-line guards (DB constraints, optimistic locking).

The API tests can't reach these paths because earlier checks catch the problem first; here
we bypass those checks to prove the database still refuses.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session
from sqlalchemy.orm.exc import StaleDataError

from netracare.config import Settings
from netracare.db.models import Slot, StaffUser, User
from netracare.db.session import make_session_factory
from netracare.domain.actor import CurrentUser
from netracare.domain.enums import AppointmentType, Role
from netracare.errors import ConflictError, ForbiddenError, ValidationAppError
from netracare.services.approval_service import ApprovalService
from netracare.services.booking_service import BookCommand, BookingService
from tests.fixtures_api import (
    FRONTDESK,
    FRONTDESK2,
    did,
    local,
    make_settings,
    patient_email,
    pid,
    slot_ids_at,
)
from tests.helpers import MutableClock


@pytest.fixture
def settings(api_db_url: str) -> Settings:
    return make_settings(api_db_url)


@pytest.fixture
def s(api_engine: Engine) -> Iterator[Session]:
    with make_session_factory(api_engine)() as session:
        yield session


def _actor(s: Session, email: str) -> CurrentUser:
    u = s.scalar(select(User).where(User.email == email))
    assert u is not None
    st = s.scalar(select(StaffUser).where(StaffUser.user_id == u.id))
    return CurrentUser(
        u.id, u.role, u.email, st.display_name if st else email, st.doctor_id if st else None
    )


def _cmd(s: Session, patient: str, hour: int, **kw: object) -> BookCommand:
    return BookCommand(
        patient_id=pid(patient),
        doctor_id=did("D5"),
        slot_ids=tuple(slot_ids_at(s, "D5", local(13, hour))),
        type=AppointmentType.ROUTINE,
        **kw,  # type: ignore[arg-type]
    )


def test_db_guard_catches_hold_race(
    s: Session, settings: Settings, clock: MutableClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    svc = BookingService(s, clock, settings)
    fd = _actor(s, FRONTDESK)
    svc.book(fd, _cmd(s, "P010", 10))
    s.flush()
    # Pretend the pre-check missed the existing hold (as in a true race on Postgres).
    slots = list(s.scalars(select(Slot).where(Slot.id.in_(slot_ids_at(s, "D5", local(13, 10))))))
    monkeypatch.setattr(BookingService, "_load_slots", lambda *a, **k: slots)
    with pytest.raises(ConflictError) as e:
        svc.book(fd, _cmd(s, "P011", 10))
    assert e.value.code == "slot_unavailable"


def test_db_guard_catches_duplicate_idempotency_key(
    s: Session, settings: Settings, clock: MutableClock
) -> None:
    svc = BookingService(s, clock, settings)
    fd = _actor(s, FRONTDESK)
    svc.book(fd, _cmd(s, "P010", 10, idempotency_key="k" * 64))
    with pytest.raises(ConflictError) as e:
        svc.book(fd, _cmd(s, "P011", 11, idempotency_key="k" * 64))
    assert e.value.code == "duplicate_request"


def test_concurrent_decisions_optimistic_lock(
    api_engine: Engine, settings: Settings, clock: MutableClock
) -> None:
    factory = make_session_factory(api_engine)
    with factory() as s0:
        actor = _actor(s0, patient_email("P010"))
        cmd = BookCommand(
            patient_id=pid("P010"),
            doctor_id=did("D1"),
            slot_ids=tuple(slot_ids_at(s0, "D1", local(13, 10), n=2)),
            type=AppointmentType.SURGERY_CONSULT,
        )
        res = BookingService(s0, clock, settings).book(actor, cmd)
        assert res.approval is not None
        req_id = res.approval.id
        s0.commit()
    s1, s2 = factory(), factory()
    try:
        a1, a2 = _actor(s1, FRONTDESK), _actor(s2, FRONTDESK2)
        svc1, svc2 = ApprovalService(s1, clock, settings), ApprovalService(s2, clock, settings)
        svc2._pending(req_id)  # session 2 loads version 1 of the request + appointment ...
        s2.commit()  # end its read snapshot but keep the (now stale) objects in memory
        svc1.approve(a1, req_id)
        s1.commit()  # ... session 1 decides and commits version 2
        with pytest.raises((ConflictError, StaleDataError)):  # both map to HTTP 409
            svc2.reject(a2, req_id)
        s2.rollback()
    finally:
        s1.close()
        s2.close()


def test_service_role_guards(s: Session, settings: Settings, clock: MutableClock) -> None:
    svc = BookingService(s, clock, settings)
    doctor = CurrentUser("d", Role.DOCTOR, "d@x", "Dr", did("D5"))
    with pytest.raises(ForbiddenError):
        svc.book(doctor, _cmd(s, "P010", 10))
    fd = _actor(s, FRONTDESK)
    with pytest.raises(ValidationAppError):
        svc.book(fd, _cmd(s, "P010", 10, override=True, override_reason="  "))
    patient = _actor(s, patient_email("P010"))
    with pytest.raises(ForbiddenError):
        svc.check_in(patient, "x")
    with pytest.raises(ForbiddenError):
        ApprovalService(s, clock, settings).approve(patient, "x")
