"""Understanding one booking turn: `booking_nlu@v1` (fast model), or keywords if it is down.

The output is extraction only (phrases as written). Code resolves dates, doctors and option
choices; code alone decides whether a message is a "yes" (see confirm.py).
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from datetime import date, datetime
from typing import Any

from netracare.agents.booking.confirm import is_explicit_yes, is_no, is_stop
from netracare.domain.relative_dates import resolve_dates, resolve_time
from netracare.guardrails.redflag_rules import looks_clinical
from netracare.llm.client import LLMClient
from netracare.llm.schemas import BookingTurn
from netracare.llm.types import LLMError
from netracare.log import get_logger

log = get_logger(__name__)

_RESCHEDULE = re.compile(
    r"\b(reschedul\w*|move|shift|change (my|the) (appointment|time|date)|postpone|prepone)\b", re.I
)
_CANCEL = re.compile(r"\b(cancel\w*|call off)\b", re.I)
_LIST = re.compile(
    r"\b(my appointments?|what appointments|when is my|upcoming (visit|appointment)s?"
    r"|do i have (an|any) appointment)\b",
    re.I,
)
_BOOK = re.compile(
    r"\b(book\w*|appointment|slot|see (a|the) doctor|visit|check ?-?up|consult\w*)\b", re.I
)
_ORDINALS = {
    "first": 1,
    "1st": 1,
    "second": 2,
    "2nd": 2,
    "third": 3,
    "3rd": 3,
    "fourth": 4,
    "4th": 4,
    "fifth": 5,
    "5th": 5,
}
_OPTION_ID = re.compile(r"^option:(\S+)$")


def is_list_request(text: str) -> bool:
    """ "What are my appointments?" - recognised by code too (the LLM sometimes says none)."""
    return bool(_LIST.search(text)) and not _RESCHEDULE.search(text) and not _CANCEL.search(text)


def keyword_fallback(text: str, current_action: str = "none") -> BookingTurn:
    t = text.strip()
    if _RESCHEDULE.search(t):
        action = "reschedule"
    elif _CANCEL.search(t) and not is_stop(t):
        action = "cancel"
    elif _LIST.search(t):
        action = "list"
    elif _BOOK.search(t):
        action = "book"
    else:
        action = current_action if current_action in ("book", "reschedule", "cancel") else "none"
    if is_explicit_yes(t):
        kind = "yes"
    elif is_stop(t):
        kind = "stop"
    elif is_no(t):
        kind = "no"
    else:
        kind = "answer"
    today = date.today()
    has_date = resolve_dates(t, today) is not None
    has_time = resolve_time(t) is not None
    if kind == "answer" and (has_date or has_time) and current_action != "none":
        kind = "change"
    return BookingTurn(
        action=action,
        doctor_mention=t if re.search(r"\b(dr\.?|doctor)\s+\w", t, re.I) else None,
        specialty_hint=None,
        # The message itself becomes the visit reason when it sounds clinical, so the booking
        # service's red-flag triage still sees it when the LLM is unavailable.
        concern_phrase=t[:80] if looks_clinical(t) and not is_explicit_yes(t) else None,
        date_phrase=t if has_date else None,
        time_phrase=t if has_time else None,
        patient_mention=None,
        appointment_ref=None,
        option_choice=None,
        appt_type=None,
        reply_kind=kind,
        confidence=0.4,
    )


def understand(
    llm: LLMClient | None,
    text: str,
    state_summary: str,
    *,
    current_action: str = "none",
    actor_user_id: str | None = None,
    redact_names: Sequence[str] = (),
) -> tuple[BookingTurn, list[str], float]:
    """(turn, llm_call_ids, cost). Falls back to keywords when the LLM is unavailable."""
    if llm is None or not llm.available:
        return keyword_fallback(text, current_action), [], 0.0
    try:
        result = llm.generate(
            "booking_nlu",
            {"message": text, "state": state_summary or "(no booking in progress)"},
            purpose="booking_nlu",
            schema=BookingTurn,
            actor_user_id=actor_user_id,
            redact_names=redact_names,
        )
    except LLMError as exc:
        log.warning("agent.nlu_fallback", code=exc.code)
        return keyword_fallback(text, current_action), [], 0.0
    return result.value, [result.call_id], float(result.cost_usd)


def _start(option: dict[str, Any]) -> datetime:
    return datetime.fromisoformat(str(option["start_local"]))


def choose_option(
    text: str, turn: BookingTurn | None, options: Sequence[dict[str, Any]]
) -> int | None:
    """0-based index of the option the patient picked, or None."""
    if not options:
        return None
    t = text.strip().lower()
    m = _OPTION_ID.match(t)
    if m:
        return next((i for i, o in enumerate(options) if o["id"] == m.group(1)), None)
    n = turn.option_choice if turn else None
    if n is None:
        m = re.fullmatch(r"(?:option|number|no\.?|#)?\s*(\d)\W*", t)
        if m:
            n = int(m.group(1))
    if n is None:
        for word, k in _ORDINALS.items():
            if re.search(rf"\b{word}\b", t):
                n = k
                break
        if re.search(r"\b(last|final)\b", t):
            n = len(options)
    if n is not None:
        return n - 1 if 1 <= n <= len(options) else None
    window = resolve_time(t)  # "the 10:30 one", "4pm"
    if window is not None and not window.out_of_hours:
        hits = [i for i, o in enumerate(options) if window.start <= _start(o).time() <= window.end]
        if len(hits) == 1:
            return hits[0]
    return None
