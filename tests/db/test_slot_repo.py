from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy.orm import Session

from netracare.domain.enums import SlotStatus
from netracare.domain.slot_generation import WorkBlock, generate_slots
from netracare.repositories.slot_repo import SlotRepository
from tests.db.factories import make_doctor

pytestmark = pytest.mark.db
IST = ZoneInfo("Asia/Kolkata")


def _specs(doctor_id: str):  # type: ignore[no-untyped-def]
    return generate_slots(
        doctor_id, [WorkBlock(0, time(9), time(11))], [], set(), date(2026, 11, 9), 7, IST
    )


def test_upsert_is_idempotent(session: Session) -> None:
    doc = make_doctor(session)
    repo = SlotRepository(session)
    assert repo.upsert_many(_specs(doc.id)) == 8
    assert repo.upsert_many(_specs(doc.id)) == 0
    assert repo.count() == 8


def test_upsert_empty() -> None:
    assert SlotRepository(None).upsert_many([]) == 0  # type: ignore[arg-type]


def test_block_range_blocks_overlapping_slots(session: Session) -> None:
    doc = make_doctor(session)
    repo = SlotRepository(session)
    repo.upsert_many(_specs(doc.id))
    start = datetime(2026, 11, 9, 9, 10, tzinfo=IST)  # overlaps 09:00 slot
    end = datetime(2026, 11, 9, 9, 30, tzinfo=IST)  # through 09:15 slot
    assert repo.block_range(doc.id, start, end) == 2
    slots = repo.for_doctor_between(doc.id, start - timedelta(hours=1), start + timedelta(hours=3))
    statuses = [(s.start_utc.astimezone(IST).time(), s.status) for s in slots]
    assert statuses[0] == (time(9, 0), SlotStatus.BLOCKED)
    assert statuses[1] == (time(9, 15), SlotStatus.BLOCKED)
    assert statuses[2] == (time(9, 30), SlotStatus.OPEN)
