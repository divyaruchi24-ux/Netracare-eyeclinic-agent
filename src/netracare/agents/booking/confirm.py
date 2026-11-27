"""Deterministic "did the patient explicitly agree?" checks (no LLM).

A write happens only when `is_explicit_yes` returns True for the message that answers a
read-back. It is deliberately strict: after removing polite filler, the *whole* message must
be agreement. A hedge, a negation, a question or any new detail ("yes but make it 11",
"yes at 4?", "ok maybe") is not a yes. The UI's confirm button sends the literal "yes".
"""

from __future__ import annotations

import re

_YES = (
    "yes",
    "yeah",
    "yea",
    "yep",
    "yup",
    "ya",
    "yes please",
    "yess",
    "yesss",
    "confirm",
    "confirmed",
    "i confirm",
    "book it",
    "book",
    "go ahead",
    "please do",
    "do it",
    "sure",
    "ok",
    "okay",
    "ok book it",
    "okay book it",
    "ok go ahead",
    "thats fine",
    "that is fine",
    "fine",
    "sounds good",
    "perfect",
    "correct",
    "right",
    "great",
    "yes book it",
    "yes go ahead",
    "yes confirm",
    "yes thats right",
    "yes that is right",
    "yes correct",
    "haan",
    "han",
    "ha",
    "haa",
    "ji",
    "haan ji",
    "ji haan",
    "theek hai",
    "thik hai",
    "ok theek hai",
    "kar do",
    "book kar do",
    "yes book",
    "yes please book",
)
_FILLER = re.compile(
    r"\b(please|pls|plz|thanks|thank you|thankyou|ty|kindly|sir|madam|maam|dear|just|so|then"
    r"|now|that|one)\b"
)
_HEDGE = re.compile(
    r"\b(no|not|dont|don't|do not|never|but|wait|hold on|hang on|maybe|perhaps|hmm+|actually"
    r"|instead|change|different|other|another|later|unsure|not sure|if|unless|except|cancel"
    r"|stop|rather|earlier|tomorrow|today|week|morning|afternoon|evening|am|pm"
    r"|monday|tuesday|wednesday|thursday|friday|saturday|sunday|nahi|nahin|mat)\b"
)
_STOP = re.compile(
    r"\b(never ?mind|forget (it|about it)|leave it|no thanks|no thank you|stop( it)?|quit|abort"
    r"|exit|don'?t bother|dont bother|don'?t book|do not book|cancel (that|this) request"
    r"|not (now|anymore|any more)|rehne do|rehnedo|chhodo|chodo)\b"
)
_NO = re.compile(r"^(no|nope|nah|nahi|nahin|no no|not really|negative)\b")


def _clean(text: str) -> str:
    t = text.lower().replace("’", "'").replace("'", "")
    t = re.sub(r"[^a-z0-9?\s]", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def is_explicit_yes(text: str) -> bool:
    t = _clean(text)
    if not t or "?" in t or re.search(r"\d", t) or _HEDGE.search(t):
        return False
    core = re.sub(r"\s+", " ", _FILLER.sub(" ", t)).strip()
    return core in _YES


def is_stop(text: str) -> bool:
    return bool(_STOP.search(_clean(text)))


def is_no(text: str) -> bool:
    t = _clean(text)
    return bool(_NO.match(t)) and not is_explicit_yes(text)
