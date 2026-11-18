"""Domain enumerations."""

from __future__ import annotations

from enum import StrEnum


class Role(StrEnum):
    PATIENT = "patient"
    FRONT_DESK = "front_desk"
    DOCTOR = "doctor"
    ADMIN = "admin"


class AppointmentStatus(StrEnum):
    REQUESTED = "requested"
    PENDING_APPROVAL = "pending_approval"
    CONFIRMED = "confirmed"
    CHECKED_IN = "checked_in"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    REJECTED = "rejected"
    NO_SHOW = "no_show"
    RESCHEDULED = "rescheduled"


ACTIVE_STATUSES: frozenset[AppointmentStatus] = frozenset(
    {
        AppointmentStatus.REQUESTED,
        AppointmentStatus.PENDING_APPROVAL,
        AppointmentStatus.CONFIRMED,
        AppointmentStatus.CHECKED_IN,
    }
)
TERMINAL_STATUSES: frozenset[AppointmentStatus] = frozenset(
    set(AppointmentStatus) - ACTIVE_STATUSES
)


class AppointmentEvent(StrEnum):
    SUBMIT_FOR_APPROVAL = "submit_for_approval"
    AUTO_CONFIRM = "auto_confirm"
    APPROVE = "approve"
    REJECT = "reject"
    CANCEL = "cancel"
    RESCHEDULE = "reschedule"
    CHECK_IN = "check_in"
    MARK_NO_SHOW = "mark_no_show"
    COMPLETE = "complete"


class AppointmentType(StrEnum):
    ROUTINE = "routine"
    FOLLOW_UP = "follow_up"
    NEW_PATIENT = "new_patient"
    SURGERY_CONSULT = "surgery_consult"

    @property
    def duration_minutes(self) -> int:
        return 30 if self in (AppointmentType.NEW_PATIENT, AppointmentType.SURGERY_CONSULT) else 15


class SlotStatus(StrEnum):
    OPEN = "open"
    BLOCKED = "blocked"


class DocType(StrEnum):
    DOCTOR_BIO = "doctor_bio"
    PROCEDURE_FAQ = "procedure_faq"
    PRICE_LIST = "price_list"
    INSURANCE = "insurance"
    PREP_INSTRUCTIONS = "prep_instructions"
    POLICY = "policy"
    GENERAL_FAQ = "general_faq"


class ActorType(StrEnum):
    USER = "user"
    AGENT = "agent"
    SYSTEM = "system"


class RedFlagLevel(StrEnum):
    EMERGENCY = "emergency"
    URGENT = "urgent"
    ROUTINE = "routine"
    NON_CLINICAL = "non_clinical"


class ApprovalReason(StrEnum):
    WITHIN_24H_CHANGE = "within_24h_change"
    NEW_PATIENT_SURGERY_CONSULT = "new_patient_surgery_consult"
    GUARDIAN_BOOKING_MINOR = "guardian_booking_minor"
    POST_RED_FLAG = "post_red_flag"
    NO_SHOW_FLAGGED = "no_show_flagged"


class NotificationChannel(StrEnum):
    SMS = "sms"
    EMAIL = "email"


class NotificationStatus(StrEnum):
    QUEUED = "queued"
    SENT = "sent"
    FAILED = "failed"
