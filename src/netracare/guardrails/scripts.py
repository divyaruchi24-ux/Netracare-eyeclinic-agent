"""Verbatim patient-facing texts from `docs/safety_policy.md` (sections 5, 6 and 10).

These are constants, never generated. `tests/unit/guardrails/test_scripts_match_policy.py`
asserts they equal the policy text character for character (markdown `> ` quote markers
aside), so editing one without the other fails CI.
"""

from __future__ import annotations

from collections.abc import Iterable

EMERGENCY_HEADLINE = "**This could be an emergency. Please don't wait for an appointment.**"
EMERGENCY_BULLETS = (
    "• Call the **NetraCare emergency line: +91 80 0000 9999** (24×7), or\n"
    "• Call **112** or go to the **nearest emergency department** now."
)
EMERGENCY_CLOSING = (
    "I've alerted our front-desk team. I can't assess symptoms, but a doctor needs to see you "
    "as soon as possible."
)
EMERGENCY_SCRIPT = f"{EMERGENCY_HEADLINE}\n{EMERGENCY_BULLETS}\n{EMERGENCY_CLOSING}"

CHEMICAL_RINSE_LINE = (
    "If a chemical has gone into the eye, **rinse the eye immediately with plenty of clean "
    "running water for at least 15 minutes** while you or someone else calls for help."
)

URGENT_SCRIPT = (
    "Thanks for telling me. Symptoms like this should be checked soon, so I've asked our "
    "front-desk team to contact you **today**. If things get worse — for example sudden loss "
    "of vision, a curtain over your vision, severe pain, or flashes with new floaters — call "
    "**+91 80 0000 9999** or **112** right away."
)

MEDICAL_REFUSAL = (
    "I'm not able to give medical advice about {topic}, but this is something our doctors can "
    "help with. {routing_sentence} Would you like me to find an appointment?"
)

LOW_CONFIDENCE = (
    "I don't have reliable information on that, and I don't want to guess. I can connect you "
    "with our front desk — would you like a call back, or would you prefer the phone number?"
)

ROUTING_TEMPLATES: dict[str, str] = {
    "specialty": (
        "Concerns about **{concern}** are typically seen by our **{specialty}** specialist, "
        "**{doctor}**."
    ),
    "pediatric": (
        "For children's eye concerns, our pediatric specialist **Dr. Arjun Menon** is usually "
        "the right first visit."
    ),
    "general": (
        "Since this could relate to more than one area, a general eye check-up with **Dr. Kavya "
        "Nair** is a good starting point; she'll refer you within the clinic if needed."
    ),
}
CHECK_AVAILABILITY = "Would you like me to check availability?"

NON_CLINICAL_HANDOFF = (
    "I can help with bookings and general clinic questions. For anything else, our front desk "
    "is happy to help on +91 80 0000 1234."
)

DISCLAIMERS: dict[str, str] = {
    "chat_footer": (
        "NetraCare Assistant is an AI that helps with clinic information and appointments. It "
        "cannot give medical advice. In an emergency call +91 80 0000 9999 or 112."
    ),
    "kb_answer": "General clinic information, not medical advice.",
    "price": (
        "Prices are estimates from our current price list (effective {date}); final charges "
        "depend on your doctor's assessment."
    ),
    "summary_draft": (
        "AI-generated DRAFT — review and edit before approving. Not visible to the patient "
        "until approved."
    ),
    "summary_approved": (
        "Summary approved by {doctor} on {date}. Contact the clinic if anything is unclear."
    ),
    "eye_fact": "General eye-health fact. Not medical advice.",
    "lab_notification": "Your report is ready. Your doctor will review it with you.",
}

# Display names used in the routing sentence (short forms of the seeded specialty names).
SPECIALTY_LABELS: dict[str, str] = {
    "CORNEA": "Cornea",
    "RETINA": "Retina",
    "GLAUCOMA": "Glaucoma",
    "PEDIATRIC": "Pediatric",
    "GENERAL": "General Ophthalmology",
}


def emergency_message(codes: Iterable[str]) -> str:
    """The section 5.1 script; for chemical exposure (E4) the rinse line goes before the
    bullets, as the policy requires."""
    if "E4" in set(codes):
        return (
            f"{EMERGENCY_HEADLINE}\n{CHEMICAL_RINSE_LINE}\n{EMERGENCY_BULLETS}\n{EMERGENCY_CLOSING}"
        )
    return EMERGENCY_SCRIPT


def routing_sentence(
    specialty_code: str | None, concern_phrase: str | None, doctor_name: str | None
) -> str:
    """A section 6 routing template; ambiguous or missing inputs route to General (D5)."""
    code = (specialty_code or "GENERAL").upper()
    if code == "PEDIATRIC":
        return ROUTING_TEMPLATES["pediatric"]
    concern = (concern_phrase or "").strip()
    if code == "GENERAL" or not concern or not doctor_name or code not in SPECIALTY_LABELS:
        return ROUTING_TEMPLATES["general"]
    return ROUTING_TEMPLATES["specialty"].format(
        concern=concern, specialty=SPECIALTY_LABELS[code], doctor=doctor_name
    )


# -- chat (Phase 3) ------------------------------------------------------------------------------
# Fixed chat replies. They are not quoted in the policy, but follow it: no advice, no claim to be
# human (section 9), front-desk number from section 12, and handoff wording from section 5.4.

FRONT_DESK_PHONE = "+91 80 0000 1234"
CHAT_FOOTER = DISCLAIMERS["chat_footer"]

EXTRACTIVE_INTRO = "Here's what our clinic information says:"

CLARIFY_TEMPLATE = (
    "To point you to the right specialist, could you tell me whether this is mainly about "
    "{options}?"
)

BOOKING_HANDOFF = (
    "You can book on the **Book appointment** page. I've filled in the suggested doctor for you."
)
BOOKING_HANDOFF_GENERIC = "You can book an appointment on the **Book appointment** page."

GREETING_REPLY = (
    "Hello! I'm NetraCare's AI assistant. I can answer questions about our clinic (doctors, "
    "timings, prices, insurance and preparing for a visit) and point you to the right "
    "specialist. How can I help?"
)
THANKS_REPLY = "You're welcome! Is there anything else I can help with?"
IDENTITY_REPLY = (
    "I'm an AI assistant for NetraCare, not a person. I can help with clinic information and "
    "appointments, and I can connect you with our front-desk team at any time."
)
OFF_TOPIC_REPLY = NON_CLINICAL_HANDOFF
EMPTY_REPLY = 'Please type your question, for example "What are your opening hours?"'

HANDOFF_CREATED = (
    "I've asked our front-desk team to pick up this conversation. They'll contact you soon. "
    f"You can also call them on {FRONT_DESK_PHONE}."
)
ANON_HANDOFF = (
    f"You can reach our front desk on {FRONT_DESK_PHONE} (Monday to Saturday, 09:00 to 18:00). "
    "If you sign in, I can also ask them to contact you."
)
FRUSTRATION_OFFER = (
    "I'm sorry this hasn't been helpful. Would you like me to ask our front-desk team to "
    "contact you?"
)
SPAM_END = (
    "It looks like the same message has been sent several times, so I'll pause this "
    f"conversation. You can start a new one at any time, or call our front desk on "
    f"{FRONT_DESK_PHONE}."
)
TURN_LIMIT_END = (
    "This conversation has reached its length limit. Please start a new conversation, or call "
    f"our front desk on {FRONT_DESK_PHONE}."
)
BUDGET_END = (
    "I can't continue this conversation right now, so I've asked our front-desk team to follow "
    f"up. You can also call them on {FRONT_DESK_PHONE}."
)
SESSION_ENDED = f"This conversation has ended. Please start a new one, or call {FRONT_DESK_PHONE}."

# The section 6 specialty template filled for Cataract & General Ophthalmology (D5) when the
# concern clearly belongs there (cataract, eyelids), instead of the "more than one area" wording.
GENERAL_SPECIALTY_LABEL = "Cataract & General Ophthalmology"


def specialty_sentence(label: str, concern: str, doctor: str) -> str:
    return ROUTING_TEMPLATES["specialty"].format(concern=concern, specialty=label, doctor=doctor)
