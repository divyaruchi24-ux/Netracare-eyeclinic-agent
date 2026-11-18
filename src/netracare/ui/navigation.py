"""Which pages each role sees (pure data, unit-testable without Streamlit)."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PageSpec:
    file: str
    title: str
    icon: str
    help: str
    default: bool = False


LOGIN = PageSpec("login.py", "Sign in", ":material/login:", "Sign in to manage appointments.", True)
REGISTER = PageSpec(
    "register.py", "Register", ":material/person_add:", "Create a patient account (18+)."
)
MESSAGES = PageSpec(
    "notifications.py", "Messages", ":material/sms:", "Simulated SMS and email notifications."
)

PATIENT = [
    PageSpec(
        "patient_appointments.py",
        "My appointments",
        ":material/event:",
        "Upcoming and past visits; cancel, reschedule or withdraw requests.",
        True,
    ),
    PageSpec(
        "patient_book.py",
        "Book a visit",
        ":material/event_available:",
        "Find a free time with a doctor and book it.",
    ),
    MESSAGES,
]
DOCTOR = [
    PageSpec(
        "doctor_schedule.py",
        "My schedule",
        ":material/calendar_month:",
        "Your clinic slots and booked patients; mark visits complete.",
        True,
    )
]
FRONT_DESK = [
    PageSpec(
        "frontdesk_today.py",
        "Today",
        ":material/today:",
        "Today's queue by doctor; check in, no-show and complete.",
        True,
    ),
    PageSpec(
        "frontdesk_approvals.py",
        "Approvals",
        ":material/rule:",
        "Bookings and changes waiting for front-desk approval, overdue first.",
    ),
    PageSpec(
        "frontdesk_walk_in.py",
        "Walk-in",
        ":material/directions_walk:",
        "Register a walk-in patient into the next free slot today.",
    ),
    PageSpec(
        "frontdesk_patients.py",
        "Patients",
        ":material/groups:",
        "Search patients, link guardians and book on a patient's behalf.",
    ),
    MESSAGES,
]
ADMIN = [
    PageSpec(
        "admin_doctors.py", "Doctors", ":material/stethoscope:", "Add, edit or deactivate doctors."
    ),
    PageSpec(
        "admin_schedule.py",
        "Schedules",
        ":material/edit_calendar:",
        "Weekly blocks, leaves and holidays; see affected appointments.",
    ),
    PageSpec("admin_users.py", "Users", ":material/manage_accounts:", "Staff accounts and access."),
    PageSpec(
        "admin_audit.py", "Audit log", ":material/history:", "Who did what, when, with request IDs."
    ),
]


def pages_for(role: str | None) -> dict[str, list[PageSpec]]:
    if role is None:
        return {"": [LOGIN, REGISTER]}
    if role == "patient":
        return {"": PATIENT}
    if role == "doctor":
        return {"": DOCTOR}
    if role == "front_desk":
        return {"Front desk": FRONT_DESK}
    if role == "admin":
        return {"Front desk": FRONT_DESK, "Admin": ADMIN}
    return {"": [LOGIN]}
