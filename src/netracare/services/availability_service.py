"""Find bookable options: open, unheld, future slots (consecutive pairs for 30-min visits)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from netracare.db.models import AppointmentSlot, Doctor, Slot, Specialty
from netracare.domain.clock import ensure_utc
from netracare.domain.enums import AppointmentType, SlotStatus
from netracare.errors import NotFoundError, ValidationAppError
from netracare.observability.tracing import traced

MAX_RANGE_DAYS = 31
SLOT_MINUTES = 15


@dataclass(frozen=True)
class AvailableOption:
    doctor_id: str
    start_utc: datetime
    end_utc: datetime
    slot_ids: tuple[str, ...]


def slots_needed(appt_type: AppointmentType) -> int:
    return appt_type.duration_minutes // SLOT_MINUTES


def group_consecutive(slots: list[Slot], n: int) -> list[AvailableOption]:
    """All runs of `n` back-to-back slots for the same doctor (input sorted by doctor, start)."""
    out: list[AvailableOption] = []
    for i in range(len(slots) - n + 1):
        window = slots[i : i + n]
        same_doctor = all(s.doctor_id == window[0].doctor_id for s in window)
        contiguous = all(window[j].end_utc == window[j + 1].start_utc for j in range(n - 1))
        if same_doctor and contiguous:
            out.append(
                AvailableOption(
                    doctor_id=window[0].doctor_id,
                    start_utc=window[0].start_utc,
                    end_utc=window[-1].end_utc,
                    slot_ids=tuple(s.id for s in window),
                )
            )
    return out


class AvailabilityService:
    def __init__(self, session: Session, tz: ZoneInfo) -> None:
        self.s = session
        self.tz = tz

    @traced(
        "availability.find",
        tags=["availability"],
        inputs=lambda self, **kw: {
            k: kw.get(k)
            for k in ("doctor_id", "specialty_code", "from_date", "to_date", "appt_type")
        },
        outputs=lambda r: {"options": len(r)},
    )
    def find(
        self,
        *,
        doctor_id: str | None,
        specialty_code: str | None,
        from_date: date,
        to_date: date,
        appt_type: AppointmentType,
        now: datetime,
    ) -> list[AvailableOption]:
        if not doctor_id and not specialty_code:
            raise ValidationAppError(
                "doctor_or_specialty_required", "choose a doctor or a specialty"
            )
        today = ensure_utc(now).astimezone(self.tz).date()
        from_date = max(from_date, today)
        if to_date < from_date:
            raise ValidationAppError("invalid_range", "to_date must not be before from_date")
        if (to_date - from_date).days + 1 > MAX_RANGE_DAYS:
            raise ValidationAppError(
                "range_too_long", f"search at most {MAX_RANGE_DAYS} days at once"
            )

        doctors = select(Doctor.id).where(Doctor.is_active)
        if doctor_id:
            if self.s.get(Doctor, doctor_id) is None:
                raise NotFoundError("doctor_not_found", "doctor not found")
            doctors = doctors.where(Doctor.id == doctor_id)
        if specialty_code:
            if self.s.scalar(select(Specialty.id).where(Specialty.code == specialty_code)) is None:
                raise NotFoundError("specialty_not_found", "specialty not found")
            doctors = doctors.join(Specialty).where(Specialty.code == specialty_code)

        start = ensure_utc(datetime.combine(from_date, time(0), self.tz))
        end = ensure_utc(datetime.combine(to_date + timedelta(days=1), time(0), self.tz))
        held = select(AppointmentSlot.slot_id).where(AppointmentSlot.active)
        slots = list(
            self.s.scalars(
                select(Slot)
                .where(
                    Slot.doctor_id.in_(doctors),
                    Slot.status == SlotStatus.OPEN,
                    Slot.start_utc >= start,
                    Slot.start_utc > ensure_utc(now),  # same rule as booking
                    Slot.start_utc < end,
                    Slot.id.not_in(held),
                )
                .order_by(Slot.doctor_id, Slot.start_utc)
            )
        )
        options = group_consecutive(slots, slots_needed(appt_type))
        return sorted(options, key=lambda o: (o.start_utc, o.doctor_id))
