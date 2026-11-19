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

    def severity(self) -> int:
        """Ordering for "most severe wins": non_clinical 0 < routine 1 < urgent 2 < emergency 3."""
        return _SEVERITY[self]

    def escalate(self) -> RedFlagLevel:
        """One level more severe (emergency stays emergency)."""
        return _BY_SEVERITY[min(self.severity() + 1, 3)]

    @staticmethod
    def most_severe(*levels: RedFlagLevel) -> RedFlagLevel:
        return max(levels, key=lambda lv: lv.severity())


_SEVERITY: dict[RedFlagLevel, int] = {
    RedFlagLevel.NON_CLINICAL: 0,
    RedFlagLevel.ROUTINE: 1,
    RedFlagLevel.URGENT: 2,
    RedFlagLevel.EMERGENCY: 3,
}
_BY_SEVERITY: dict[int, RedFlagLevel] = {v: k for k, v in _SEVERITY.items()}

TriageLevel = RedFlagLevel


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


class ApprovalKind(StrEnum):
    NEW_BOOKING = "new_booking"
    RESCHEDULE = "reschedule"
    CANCEL = "cancel"


class ApprovalStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    WITHDRAWN = "withdrawn"


class AlertStatus(StrEnum):
    OPEN = "open"
    ACKNOWLEDGED = "acknowledged"
    RESOLVED = "resolved"


class SummaryStatus(StrEnum):
    DRAFT = "draft"
    APPROVED = "approved"
    DISCARDED = "discarded"


class LLMCallStatus(StrEnum):
    OK = "ok"
    ERROR = "error"
    BUDGET_BLOCKED = "budget_blocked"
    INVALID_OUTPUT = "invalid_output"
