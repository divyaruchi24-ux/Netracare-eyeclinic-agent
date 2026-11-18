from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import select, update

from netracare.db.models import Appointment, AppointmentSlot
from netracare.repositories.base import Repository


class AppointmentRepository(Repository[Appointment]):
    model = Appointment

    def hold_slots(self, appointment: Appointment, slot_ids: Sequence[str]) -> None:
        """Attach active holds. Raises IntegrityError if any slot already has an active hold."""
        for slot_id in slot_ids:
            self.session.add(
                AppointmentSlot(appointment_id=appointment.id, slot_id=slot_id, active=True)
            )
        self.session.flush()

    def release_slots(self, appointment_id: str) -> int:
        result = self.session.execute(
            update(AppointmentSlot)
            .where(AppointmentSlot.appointment_id == appointment_id, AppointmentSlot.active)
            .values(active=False)
        )
        return int(result.rowcount or 0)  # type: ignore[attr-defined]

    def by_idempotency_key(self, key: str) -> Appointment | None:
        return self.session.scalar(select(Appointment).where(Appointment.idempotency_key == key))

    def for_patient(self, patient_id: str) -> list[Appointment]:
        stmt = (
            select(Appointment)
            .where(Appointment.patient_id == patient_id)
            .order_by(Appointment.start_utc)
        )
        return list(self.session.scalars(stmt))
