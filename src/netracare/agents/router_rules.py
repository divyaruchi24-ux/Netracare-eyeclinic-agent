"""Layer 1 of the chat router: deterministic checks that run on every message, before any LLM.

Order matters (docs/phase-3/understanding.md 3.8):
1. empty / emoji-only, and spam (the same message 3+ times in a session);
2. red-flag rules (Phase 2): emergency or urgent overrides everything, even a booking request;
3. medical-advice requests and pasted lab reports / prescriptions -> section 5.3 refusal;
4. explicit requests for a human -> handoff;
5. trivial small talk (greetings, thanks, "are you a bot?") -> fixed replies, no LLM call.
Prompt-injection patterns are only flagged (section 9: log and continue normally).

The LLM router (layer 2) never sees a message that layer 1 decided, so it cannot downgrade it.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field

from netracare.domain.enums import ChatIntent, RedFlagLevel
from netracare.guardrails.redflag_rules import asks_medical_advice, detect, level_for, normalize
from netracare.rag.injection import scan_text

SPAM_REPEATS = 3  # the 3rd identical message in a session ends it

_HAS_TEXT = re.compile(r"[A-Za-z0-9ऀ-෿]")

LAB_REPORT = re.compile(
    r"\b(?:my|the|this|these) (?:eye |lab |test |blood |scan )?"
    r"(?:report|results?|reading|readings|scan|oct|prescription|power|numbers?|levels?|pressure"
    r"|iop|sugar|hba1c|values?)\b.{0,60}?\b(?:says?|shows?|mean|means|bad|good|high|low|normal"
    r"|ok|okay|fine|serious|dangerous|worrying|worry|concerning|interpret|explain)\b"
    r"|\b(?:iop|hba1c|sph|cyl|axis|mmhg|od|os)\b\s*[:=]?\s*[-+]?\d"
    r"|\b(?:interpret|explain|read)\b.{0,30}\b(?:report|results?|prescription|scan|oct)\b",
    re.IGNORECASE,
)

HUMAN_REQUEST = re.compile(
    r"\b(?:talk|speak|chat) (?:to|with) (?:a |an |the |your )?(?:human|person|someone|staff"
    r"|receptionist|real person|agent|front ?desk|doctor's office)\b"
    r"|\b(?:call me(?: back)?|callback|call back|ring me)\b|\breal (?:person|human)\b"
    r"|\bhuman,? please\b|\b(?:connect|transfer) me\b",
    re.IGNORECASE,
)

GREETING = re.compile(
    r"^\s*(?:hi+|hello+|hey+|hiya|namaste|namaskara|good (?:morning|afternoon|evening))"
    r"(?: there| netracare)?\s*[!.,:)]*\s*$",
    re.IGNORECASE,
)
THANKS = re.compile(
    r"^\s*(?:ok(?:ay)?[, ]*)?(?:thanks?(?: a lot| so much)?|thank you(?: so much)?|thx|ty|cheers"
    r"|great,? thanks|got it,? thanks)\s*[!.]*\s*$",
    re.IGNORECASE,
)
IDENTITY = re.compile(
    r"\bare you (?:a |an )?(?:human|bot|robot|real|ai|person|machine|computer)\b|\bwho are you\b"
    r"|\bwhat are you\b",
    re.IGNORECASE,
)
_DRUG_TOPIC = re.compile(
    r"\b(?:drops?|medicine|medication|tablets?|pills?|dose|dosage|ointment|antibiotics?)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Layer1Decision:
    intent: ChatIntent | None  # None: defer to the LLM router
    reason: str = ""
    level: RedFlagLevel | None = None
    codes: tuple[str, ...] = ()
    topic: str | None = None  # section 5.3 refusal topic
    reply_key: str | None = None  # chit-chat reply constant
    injection: tuple[str, ...] = field(default_factory=tuple)


def norm_for_spam(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def refusal_topic(text: str, *, lab: bool) -> str:
    if lab:
        return "test results"
    if _DRUG_TOPIC.search(text):
        return "medicines or eye drops"
    return "your symptoms"


def layer1(text: str, previous_user_texts: Sequence[str] = ()) -> Layer1Decision:
    injection = tuple(scan_text(text))
    if not _HAS_TEXT.search(text):
        return Layer1Decision(ChatIntent.CHIT_CHAT, "empty", reply_key="empty", injection=injection)

    key = norm_for_spam(text)
    repeats = sum(1 for t in previous_user_texts if norm_for_spam(t) == key)

    matches = detect(text)
    level = level_for(matches)
    if level in (RedFlagLevel.EMERGENCY, RedFlagLevel.URGENT):
        # Safety first, even for a repeated message: the script is shown every time.
        return Layer1Decision(
            ChatIntent.EMERGENCY,
            f"redflag_{level.value}",
            level=level,
            codes=tuple(sorted({m.code for m in matches})),
            injection=injection,
        )
    if repeats + 1 >= SPAM_REPEATS:
        return Layer1Decision(ChatIntent.OFF_TOPIC, "spam", injection=injection)

    lab = bool(LAB_REPORT.search(text))
    if lab or asks_medical_advice(text):
        return Layer1Decision(
            ChatIntent.MEDICAL_QUESTION,
            "lab_report" if lab else "medical_advice",
            topic=refusal_topic(text, lab=lab),
            injection=injection,
        )
    if HUMAN_REQUEST.search(text):
        return Layer1Decision(ChatIntent.HUMAN_HANDOFF, "user_request", injection=injection)
    if GREETING.match(text):
        return Layer1Decision(ChatIntent.CHIT_CHAT, "greeting", reply_key="greeting")
    if THANKS.match(text):
        return Layer1Decision(ChatIntent.CHIT_CHAT, "thanks", reply_key="thanks")
    if IDENTITY.search(normalize(text)):
        return Layer1Decision(ChatIntent.CHIT_CHAT, "identity", reply_key="identity")
    return Layer1Decision(None, "defer", injection=injection)
