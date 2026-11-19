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
