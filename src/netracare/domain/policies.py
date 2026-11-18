"""Clinic booking policies (pure functions)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Final

from netracare.domain.clock import ensure_utc
from netracare.domain.enums import AppointmentType, ApprovalReason, RedFlagLevel

CHANGE_WINDOW: Final = timedelta(hours=24)
NO_SHOW_FLAG_THRESHOLD: Final = 3
ADULT_AGE: Final = 18


def is_within_change_window(appt_start_utc: datetime, now_utc: datetime) -> bool:
    """True when the appointment starts in less than 24h (changes then need approval).

    Exactly 24h before is still a free change.
    """
    return ensure_utc(appt_start_utc) - ensure_utc(now_utc) < CHANGE_WINDOW


def should_flag_for_no_shows(no_show_count: int) -> bool:
    return no_show_count >= NO_SHOW_FLAG_THRESHOLD


def age_on(dob: date, today: date) -> int:
    years = today.year - dob.year
    if (today.month, today.day) < (dob.month, dob.day):
        years -= 1
    return years


def is_minor(dob: date, today: date) -> bool:
    """`today` must be the clinic-local date (see `clock.clinic_today`)."""
    return age_on(dob, today) < ADULT_AGE


@dataclass(frozen=True)
class BookingContext:
    appt_start_utc: datetime
    now_utc: datetime
    appointment_type: AppointmentType
    is_change: bool = False
    is_new_patient: bool = False
    patient_is_minor: bool = False
    booked_by_guardian: bool = False
    red_flag_level: RedFlagLevel | None = None
    patient_no_show_flagged: bool = False


def requires_approval(ctx: BookingContext) -> list[ApprovalReason]:
    """Return every reason front-desk approval is required. Empty list => auto-confirm."""
    reasons: list[ApprovalReason] = []
    if ctx.is_change and is_within_change_window(ctx.appt_start_utc, ctx.now_utc):
        reasons.append(ApprovalReason.WITHIN_24H_CHANGE)
    if ctx.is_new_patient and ctx.appointment_type is AppointmentType.SURGERY_CONSULT:
        reasons.append(ApprovalReason.NEW_PATIENT_SURGERY_CONSULT)
    if ctx.patient_is_minor and ctx.booked_by_guardian:
        reasons.append(ApprovalReason.GUARDIAN_BOOKING_MINOR)
    if ctx.red_flag_level in (RedFlagLevel.EMERGENCY, RedFlagLevel.URGENT):
        reasons.append(ApprovalReason.POST_RED_FLAG)
    if ctx.patient_no_show_flagged:
        reasons.append(ApprovalReason.NO_SHOW_FLAGGED)
    return reasons
