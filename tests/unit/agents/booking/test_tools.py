from __future__ import annotations

from collections.abc import Iterator
from datetime import date

import pytest
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session

from netracare.agents.booking import tools as bt
from netracare.db.models import Appointment, ApprovalRequest, User
from netracare.db.session import make_session_factory
from netracare.domain.actor import CurrentUser
from netracare.domain.enums import AppointmentStatus, Role
from tests.fixtures_api import (
    NOW,
    did,
    local,
    make_settings,
    patient_email,
    pid,
    slot_ids_at,
)
from tests.helpers import MutableClock


@pytest.fixture
def ctx_for(api_engine: Engine, api_db_url: str) -> Iterator[object]:
    factory = make_session_factory(api_engine)
    sessions: list[Session] = []

    def make(key: str = "P010", thread: str = "t1") -> bt.ToolContext:
        s = factory()
        sessions.append(s)
        user = s.scalar(select(User).where(User.email == patient_email(key)))
        assert user is not None
        actor = CurrentUser(user.id, Role.PATIENT, user.email, user.email)
        return bt.ToolContext(s, MutableClock(NOW), make_settings(api_db_url), actor, thread)

    yield make
    for s in sessions:
        s.rollback()
        s.close()


def _count(s: Session) -> int:
    return s.scalar(select(func.count()).select_from(Appointment)) or 0


def test_search_doctors_by_specialty_and_fuzzy_name(ctx_for) -> None:  # type: ignore[no-untyped-def]
    ctx = ctx_for()
    r = bt.search_doctors(ctx, specialty="RETINA")
    assert r.ok and [d["doctor_name"] for d in r.data["doctors"]] == ["Dr. Vikram Shetty"]
    r = bt.search_doctors(ctx, name="Dr Shetti")
    assert [d["code"] for d in r.data["doctors"]] == ["D2"]
    assert len(bt.search_doctors(ctx).data["doctors"]) == 5
    assert ctx.calls == ["search_doctors"] * 3


def test_availability_returns_labelled_options(ctx_for) -> None:  # type: ignore[no-untyped-def]
    ctx = ctx_for()
    r = bt.get_availability(
        ctx, specialty="RETINA", from_date=date(2026, 11, 12), to_date=date(2026, 11, 13)
    )
    assert r.ok and r.data["options"]
    o = r.data["options"][0]
    assert o["doctor_name"] == "Dr. Vikram Shetty" and o["slot_ids"]
    assert o["start_local"].startswith("2026-11-12")


def test_invalid_arguments_and_service_errors(ctx_for) -> None:  # type: ignore[no-untyped-def]
    ctx = ctx_for()
    assert bt.get_availability(ctx, from_date="nope", to_date="x").error == "invalid_arguments"
    r = bt.get_availability(ctx, from_date=date(2026, 11, 12), to_date=date(2026, 11, 13))
    assert r.error == "doctor_or_specialty_required"


def test_book_is_idempotent_per_proposal(ctx_for, db: Session) -> None:  # type: ignore[no-untyped-def]
    ctx = ctx_for()
    slots = slot_ids_at(db, "D5", local(13, 10))
    before = _count(ctx.session)
    args = {
        "patient_id": pid("P010"),
        "doctor_id": did("D5"),
        "slot_ids": slots,
        "proposal_id": "p1",
        "reason": "routine check-up",
    }
    first = bt.book_appointment(ctx, **args)
    assert first.ok and first.data["status"] in ("confirmed", "pending_approval")
    again = bt.book_appointment(ctx, **args)
    assert again.ok and again.data["replayed"] is True
    assert again.data["appointment_id"] == first.data["appointment_id"]
    assert _count(ctx.session) == before + 1


def test_slot_taken_is_reported_without_a_write(ctx_for, db: Session) -> None:  # type: ignore[no-untyped-def]
    a = ctx_for("P010", "ta")
    slots = slot_ids_at(db, "D5", local(13, 11))
    assert bt.book_appointment(
        a, patient_id=pid("P010"), doctor_id=did("D5"), slot_ids=slots, proposal_id="x"
    ).ok
    a.session.commit()
    b = ctx_for("P011", "tb")  # (the API starts every write request with BEGIN IMMEDIATE)
    before = _count(b.session)
    r = bt.book_appointment(
        b, patient_id=pid("P011"), doctor_id=did("D5"), slot_ids=slots, proposal_id="y"
    )
    assert not r.ok and r.error in ("slot_unavailable", "slot_taken")
    assert _count(b.session) == before


def test_other_patients_are_not_reachable(ctx_for, db: Session) -> None:  # type: ignore[no-untyped-def]
    other = ctx_for("P011", "o")
    slots = slot_ids_at(db, "D5", local(13, 12))
    booked = bt.book_appointment(
        other, patient_id=pid("P011"), doctor_id=did("D5"), slot_ids=slots, proposal_id="o1"
    )
    other.session.commit()
    ctx = ctx_for("P010")
    slots2 = slot_ids_at(db, "D5", local(13, 14))
    r = bt.book_appointment(
        ctx, patient_id=pid("P011"), doctor_id=did("D5"), slot_ids=slots2, proposal_id="z"
    )
    assert not r.ok and r.error in ("patient_not_found", "forbidden")
    assert bt.get_my_appointments(ctx, patient_id=pid("P011")).error == "patient_not_found"
    r = bt.cancel_appointment(ctx, appointment_id=booked.data["appointment_id"])
    assert not r.ok and r.error == "appointment_not_found"


def test_cancel_twice_and_list(ctx_for, db: Session) -> None:  # type: ignore[no-untyped-def]
    ctx = ctx_for()
    slots = slot_ids_at(db, "D1", local(16, 10))  # > 24 h away: cancel needs no approval
    booked = bt.book_appointment(
        ctx, patient_id=pid("P010"), doctor_id=did("D1"), slot_ids=slots, proposal_id="c1"
    )
    aid = booked.data["appointment_id"]
    listed = bt.get_my_appointments(ctx)
    assert aid in [x["appointment_id"] for x in listed.data["appointments"]]
    first = bt.cancel_appointment(ctx, appointment_id=aid, reason="plans changed")
    assert first.ok and first.data["cancelled"] is True
    assert bt.cancel_appointment(ctx, appointment_id=aid).error == "already_cancelled"


def test_within_24h_cancel_becomes_a_request(ctx_for, db: Session) -> None:  # type: ignore[no-untyped-def]
    ctx = ctx_for()
    slots = slot_ids_at(db, "D5", local(10, 16))  # today 16:00, now is 09:00
    booked = bt.book_appointment(
        ctx, patient_id=pid("P010"), doctor_id=did("D5"), slot_ids=slots, proposal_id="w"
    )
    assert booked.ok
    r = bt.cancel_appointment(ctx, appointment_id=booked.data["appointment_id"])
    assert r.ok and r.data["needs_approval"] and r.data["approval_request_id"]
    assert ctx.session.get(ApprovalRequest, r.data["approval_request_id"]) is not None


def test_reschedule_idempotent(ctx_for, db: Session) -> None:  # type: ignore[no-untyped-def]
    ctx = ctx_for()
    first = bt.book_appointment(
        ctx,
        patient_id=pid("P010"),
        doctor_id=did("D1"),
        slot_ids=slot_ids_at(db, "D1", local(17, 10)),
        proposal_id="r0",
    )
    new_slots = slot_ids_at(db, "D1", local(19, 11))
    args = {
        "appointment_id": first.data["appointment_id"],
        "slot_ids": new_slots,
        "proposal_id": "r1",
    }
    r = bt.reschedule_appointment(ctx, **args)
    assert r.ok and r.data["appointment_id"] != first.data["appointment_id"]
    again = bt.reschedule_appointment(ctx, **args)
    assert again.data["replayed"] and again.data["appointment_id"] == r.data["appointment_id"]


def test_unexpected_failure_rolls_back_the_savepoint(
    ctx_for,
    db: Session,
    monkeypatch: pytest.MonkeyPatch,  # type: ignore[no-untyped-def]
) -> None:
    ctx = ctx_for()
    from netracare.services.booking_service import BookingService

    original = BookingService.book

    def boom(self, actor, cmd):  # type: ignore[no-untyped-def]
        original(self, actor, cmd)  # writes the appointment, then fails
        raise RuntimeError("disk full")

    monkeypatch.setattr(BookingService, "book", boom)
    before = _count(ctx.session)
    r = bt.book_appointment(
        ctx,
        patient_id=pid("P010"),
        doctor_id=did("D5"),
        slot_ids=slot_ids_at(db, "D5", local(13, 16)),
        proposal_id="f",
    )
    assert not r.ok and r.error == "internal"
    assert _count(ctx.session) == before  # no partial write


def test_emergency_reason_keeps_the_alert(ctx_for, db: Session) -> None:  # type: ignore[no-untyped-def]
    from netracare.db.models import TriageAlert

    ctx = ctx_for()
    r = bt.book_appointment(
        ctx,
        patient_id=pid("P010"),
        doctor_id=did("D5"),
        slot_ids=slot_ids_at(db, "D5", local(13, 15)),
        proposal_id="e",
        reason="sudden curtain over my vision",
    )
    assert not r.ok and r.error == "emergency_detected"
    assert ctx.session.scalar(select(func.count()).select_from(TriageAlert)) == 1
    assert (
        ctx.session.scalar(
            select(func.count())
            .select_from(Appointment)
            .where(
                Appointment.status == AppointmentStatus.CONFIRMED,
                Appointment.reason_text == "sudden curtain over my vision",
            )
        )
        == 0
    )


def test_langchain_tools_wrap_the_same_functions(ctx_for) -> None:  # type: ignore[no-untyped-def]
    tools = bt.as_langchain_tools(ctx_for())
    names = {t.name for t in tools}
    assert {"search_doctors", "get_availability", "book_appointment"} <= names
    out = next(t for t in tools if t.name == "search_doctors").invoke({"specialty": "GLAUCOMA"})
    assert out["ok"] and out["data"]["doctors"][0]["doctor_name"] == "Dr. Meera Iyer"
