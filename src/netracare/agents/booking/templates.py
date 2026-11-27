"""Fixed texts of the booking agent. Every value inside them comes from tool results or the
clinic directory, never from the LLM, so a read-back can't contain an invented time or doctor."""

from __future__ import annotations

from datetime import datetime

from netracare.domain.relative_dates import format_day
from netracare.guardrails.scripts import FRONT_DESK_PHONE

TYPE_LABELS = {
    "routine": "Routine check",
    "follow_up": "Follow-up",
    "new_patient": "New-patient visit",
    "surgery_consult": "Surgery consultation",
}
APPROVAL_REASONS = {
    "within_24h_change": "it is within 24 hours of the appointment",
    "new_patient_surgery_consult": "it is a new-patient surgery consultation",
    "guardian_booking_minor": "it is a booking for a child",
    "post_red_flag": "of the symptom you described earlier",
    "no_show_flagged": "of earlier missed appointments",
}

SIGN_IN = (
    "To book, change or cancel an appointment, please sign in first. I can still answer "
    "questions about the clinic."
)
ASK_PATIENT = "Who is the appointment for? {names}"
ASK_TARGET = (
    "Which doctor would you like to see, or what is the visit about? (For example: dry eyes, "
    "a cataract check, or Dr. Kavya Nair.)"
)
ASK_DATES = 'Which day suits you? You can say things like "next Tuesday", "tomorrow" or "12 Dec".'
ASK_APPOINTMENT = "Which appointment do you mean?\n{items}"
NO_APPOINTMENTS = "I can't find any upcoming appointments for you."
NOT_FOUND = "I can't find that appointment. Here are your upcoming ones:\n{items}"
OPTIONS_INTRO = "Here are the earliest free times{context}. Which one would you like?"
NO_OPTIONS = (
    "I couldn't find a free time{context}. Would you like me to look at other days, or another "
    "doctor in the same specialty?"
)
CLOSED_DAY = "{reason} Would another day work? For example {suggestions}."
OUT_OF_HOURS = (
    "The clinic is open 09:00 to 18:00, Monday to Saturday. Which time in that range suits you?"
)
PICK_AGAIN = (
    'Please pick one of the times above (for example "the second one"), or tell me a different '
    "day or time."
)
READBACK = (
    "Please confirm: **{type}** with **{doctor}** ({specialty}) on **{when}** for **{patient}**."
    "{approval} Shall I {verb} it? (yes / no)"
)
READBACK_CANCEL = (
    "Please confirm: cancel the **{type}** with **{doctor}** on **{when}** for **{patient}**."
    "{approval} Shall I cancel it? (yes / no)"
)
READBACK_RESCHEDULE = (
    "Please confirm: move the {type} with **{doctor}** from **{old_when}** to **{when}** for "
    "**{patient}**.{approval} Shall I move it? (yes / no)"
)
APPROVAL_NOTE = " This needs front-desk approval because {reasons}."
REPEAT_READBACK = "Just to be sure, please reply **yes** to go ahead, or tell me what to change."
CONFIRMED = (
    "Done. Your {type} with **{doctor}** is confirmed for **{when}**. You'll also get a message."
)
RESCHEDULED = (
    "Done. Your appointment with **{doctor}** is moved to **{when}**. You'll also get a message."
)
CANCELLED = "Done. Your appointment with **{doctor}** on **{when}** is cancelled."
PENDING = (
    "Thanks. Your request ({what}) is with our front desk for approval. I'll update you here "
    "and you'll get a message as soon as they decide."
)
DECIDED_APPROVED = "Good news: our front desk approved your request. {what} is confirmed."
DECIDED_CHANGED = (
    "Our front desk confirmed your request at a different time: **{when}** with **{doctor}**. "
    "If that doesn't suit you, just tell me and I'll find another time."
)
DECIDED_REJECTED = (
    "Our front desk could not approve your request ({what}).{note} Would you like me to look for "
    "another time?"
)
OVERDUE = (
    f"Our team hasn't confirmed this yet; we'll contact you, or you can call {FRONT_DESK_PHONE}."
)
STILL_PENDING = "Your request ({what}) is still waiting for the front desk. " + OVERDUE
ABORTED = (
    "No problem, I haven't booked or changed anything. Is there anything else I can help with?"
)
EXPIRED = "Your earlier booking request wasn't completed, so I've started fresh. "
PAUSED = (
    "I've paused your booking. When you're safe and getting help, reply **continue booking** if "
    "you'd still like an appointment."
)
RESUMED = "Thanks for letting me know. Let's continue with your booking."
SLOT_GONE = "Sorry, that time was just taken. Here are the next free times:"
SLOT_RECHECK = (
    "Those times were offered a while ago, so I checked again. Here are the current free times:"
)
TOOL_FAILED = (
    "Sorry, something went wrong on our side, so nothing was booked. Let me try once more."
)
ESCALATED = (
    "Sorry, I couldn't complete this, and nothing was booked or changed. I've asked our front "
    f"desk to contact you; you can also call {FRONT_DESK_PHONE}."
)
EMERGENCY_REASON = (
    "I can't book this as a routine appointment, because what you described could be urgent."
)
LIST_ITEMS = "Your upcoming appointments:\n{items}"
LIST_EMPTY = "You have no upcoming appointments. Would you like to book one?"
CONTINUE_LATER = "We can continue your booking whenever you're ready."


def when(start_local: str) -> str:
    dt = datetime.fromisoformat(start_local)
    return f"{format_day(dt.date())} at {dt:%H:%M}"


def short_when(start_local: str) -> str:
    dt = datetime.fromisoformat(start_local)
    return f"{dt:%a} {dt.day} {dt:%b}, {dt:%H:%M}"


def approval_note(reasons: list[str]) -> str:
    if not reasons:
        return ""
    text = " and ".join(APPROVAL_REASONS.get(r, r.replace("_", " ")) for r in reasons)
    return APPROVAL_NOTE.format(reasons=text)
