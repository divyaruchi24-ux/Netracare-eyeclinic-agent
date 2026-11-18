from datetime import UTC, date, datetime, timedelta

import pytest

from netracare.domain.enums import AppointmentType, ApprovalReason, RedFlagLevel
from netracare.domain.policies import (
    BookingContext,
    age_on,
    is_minor,
    is_within_change_window,
    requires_approval,
    should_flag_for_no_shows,
)

NOW = datetime(2026, 11, 10, 5, 0, tzinfo=UTC)


def test_exactly_24h_is_free() -> None:
    assert not is_within_change_window(NOW + timedelta(hours=24), NOW)


def test_23h59m_requires_approval() -> None:
    assert is_within_change_window(NOW + timedelta(hours=23, minutes=59), NOW)


def test_naive_datetime_rejected() -> None:
    with pytest.raises(ValueError):
        is_within_change_window(datetime(2026, 11, 11, 5, 0), NOW)


def test_auto_confirm_when_no_reasons() -> None:
    ctx = BookingContext(
        appt_start_utc=NOW + timedelta(days=3),
        now_utc=NOW,
        appointment_type=AppointmentType.ROUTINE,
    )
    assert requires_approval(ctx) == []


def test_new_booking_within_24h_is_not_a_change() -> None:
    ctx = BookingContext(
        appt_start_utc=NOW + timedelta(hours=2),
        now_utc=NOW,
        appointment_type=AppointmentType.ROUTINE,
    )
    assert requires_approval(ctx) == []


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        (
            {"is_change": True, "appt_start_utc": NOW + timedelta(hours=5)},
            ApprovalReason.WITHIN_24H_CHANGE,
        ),
        (
            {"is_new_patient": True, "appointment_type": AppointmentType.SURGERY_CONSULT},
            ApprovalReason.NEW_PATIENT_SURGERY_CONSULT,
        ),
        (
            {"patient_is_minor": True, "booked_by_guardian": True},
            ApprovalReason.GUARDIAN_BOOKING_MINOR,
        ),
        ({"red_flag_level": RedFlagLevel.URGENT}, ApprovalReason.POST_RED_FLAG),
        ({"red_flag_level": RedFlagLevel.EMERGENCY}, ApprovalReason.POST_RED_FLAG),
        ({"patient_no_show_flagged": True}, ApprovalReason.NO_SHOW_FLAGGED),
    ],
)
def test_each_approval_reason(overrides: dict[str, object], expected: ApprovalReason) -> None:
    base: dict[str, object] = {
        "appt_start_utc": NOW + timedelta(days=3),
        "now_utc": NOW,
        "appointment_type": AppointmentType.ROUTINE,
    }
    base.update(overrides)
    assert requires_approval(BookingContext(**base)) == [expected]  # type: ignore[arg-type]


def test_routine_red_flag_does_not_require_approval() -> None:
    ctx = BookingContext(
        appt_start_utc=NOW + timedelta(days=3),
        now_utc=NOW,
        appointment_type=AppointmentType.ROUTINE,
        red_flag_level=RedFlagLevel.ROUTINE,
    )
    assert requires_approval(ctx) == []


def test_multiple_reasons_accumulate() -> None:
    ctx = BookingContext(
        appt_start_utc=NOW + timedelta(hours=1),
        now_utc=NOW,
        appointment_type=AppointmentType.SURGERY_CONSULT,
        is_change=True,
        is_new_patient=True,
        patient_no_show_flagged=True,
    )
    assert set(requires_approval(ctx)) == {
        ApprovalReason.WITHIN_24H_CHANGE,
        ApprovalReason.NEW_PATIENT_SURGERY_CONSULT,
        ApprovalReason.NO_SHOW_FLAGGED,
    }


def test_is_minor_birthday_boundary() -> None:
    dob = date(2008, 11, 10)
    assert is_minor(dob, date(2026, 11, 9))
    assert not is_minor(dob, date(2026, 11, 10))


def test_leap_day_birthday() -> None:
    assert age_on(date(2008, 2, 29), date(2026, 2, 28)) == 17
    assert age_on(date(2008, 2, 29), date(2026, 3, 1)) == 18


@pytest.mark.parametrize(("count", "flag"), [(0, False), (2, False), (3, True), (5, True)])
def test_no_show_flag(count: int, flag: bool) -> None:
    assert should_flag_for_no_shows(count) is flag
