from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime

from sqlalchemy import select, update

from netracare.db.models import Slot
from netracare.domain.enums import SlotStatus
from netracare.domain.slot_generation import SlotSpec
from netracare.repositories.base import Repository


class SlotRepository(Repository[Slot]):
    model = Slot

    def upsert_many(self, specs: Iterable[SlotSpec]) -> int:
        """Insert slots not already present (keyed by doctor_id + start_utc); return # inserted."""
        specs = list(specs)
        if not specs:
            return 0
        doctor_ids = {s.doctor_id for s in specs}
        lo = min(s.start_utc for s in specs)
        hi = max(s.start_utc for s in specs)
        rows = self.session.execute(
            select(Slot.doctor_id, Slot.start_utc).where(
                Slot.doctor_id.in_(doctor_ids), Slot.start_utc >= lo, Slot.start_utc <= hi
            )
        )
        existing = {(doctor_id, start) for doctor_id, start in rows}
        new = [
            Slot(doctor_id=s.doctor_id, start_utc=s.start_utc, end_utc=s.end_utc)
            for s in specs
            if (s.doctor_id, s.start_utc) not in existing
        ]
        self.session.add_all(new)
        self.session.flush()
        return len(new)

    def block_range(self, doctor_id: str, start_utc: datetime, end_utc: datetime) -> int:
        """Mark slots overlapping [start, end) as blocked (e.g. a newly added leave)."""
        result = self.session.execute(
            update(Slot)
            .where(
                Slot.doctor_id == doctor_id,
                Slot.start_utc < end_utc,
                Slot.end_utc > start_utc,
                Slot.status == SlotStatus.OPEN,
            )
            .values(status=SlotStatus.BLOCKED)
        )
        return int(result.rowcount or 0)  # type: ignore[attr-defined]

    def for_doctor_between(
        self, doctor_id: str, start_utc: datetime, end_utc: datetime
    ) -> list[Slot]:
        stmt = (
            select(Slot)
            .where(
                Slot.doctor_id == doctor_id, Slot.start_utc >= start_utc, Slot.start_utc < end_utc
            )
            .order_by(Slot.start_utc)
        )
        return list(self.session.scalars(stmt))
