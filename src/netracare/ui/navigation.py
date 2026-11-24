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
CHAT = PageSpec(
    "chat.py",
    "Ask NetraCare",
    ":material/chat:",
    "Questions about the clinic, answered from our own information with sources.",
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
    PageSpec(
        "patient_symptom_check.py",
        "Symptom check",
        ":material/health_and_safety:",
        "Tell us what is going on; we point you to the right care. Not medical advice.",
    ),
    CHAT,
    MESSAGES,
]
DOCTOR = [
    PageSpec(
        "doctor_schedule.py",
        "My schedule",
        ":material/calendar_month:",
        "Your clinic slots and booked patients; mark visits complete.",
        True,
    ),
    PageSpec(
        "doctor_visit_notes.py",
        "Visit notes",
        ":material/edit_note:",
        "Turn your notes into a patient summary draft; review, edit and approve.",
    ),
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
        "frontdesk_alerts.py",
        "Alerts",
        ":material/emergency:",
        "Red-flag symptom alerts: emergencies first; acknowledge and resolve.",
    ),
    PageSpec(
        "frontdesk_handoffs.py",
        "Chat handoffs",
        ":material/support_agent:",
        "Conversations the assistant handed over: claim, call back and close.",
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
    PageSpec(
        "admin_llm_usage.py",
        "AI usage",
        ":material/monitoring:",
        "LLM calls, tokens and cost against the daily and monthly budget.",
    ),
    PageSpec(
        "admin_kb.py",
        "Knowledge base",
        ":material/library_books:",
        "Documents, chunks and the search index; re-index and debug retrieval.",
    ),
]


def pages_for(role: str | None) -> dict[str, list[PageSpec]]:
    if role is None:
        return {"": [LOGIN, REGISTER, CHAT]}
    if role == "patient":
        return {"": PATIENT}
    if role == "doctor":
        return {"": DOCTOR}
    if role == "front_desk":
        return {"Front desk": FRONT_DESK}
    if role == "admin":
        return {"Front desk": FRONT_DESK, "Admin": ADMIN}
    return {"": [LOGIN]}
