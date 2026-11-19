"""Deterministic content checks for generated text.

* `check_fact` - the eye-fact banner must be a short, general, on-topic fact: no advice, no
  medicines or doses, no numbers with units, no diseases or risk talk, no URLs.
* `check_summary` - visit-summary faithfulness: every medicine, number/unit and condition
  word in the draft must appear in the doctor's notes; flagged items need the doctor's
  attention before approval.
* `safe_concern` - an LLM-proposed concern phrase is only used in a routing sentence if it is
  short, plain and contains no condition or advice words.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

MAX_FACT_WORDS = 45

ADVICE_PATTERNS: dict[str, re.Pattern[str]] = {
    "advice_directive": re.compile(
        r"\b(you should|you must|you need to|you ought|you can (help|prevent|protect|reduce|keep|avoid)"
        r"|make sure|be sure to|remember to|don't|do not|try to|avoid\w*|stop(ping)? \w+|consider"
        r"|consult|ask your|get your eyes|schedule|book an?|it is best to|it's best to|is recommended)\b"
        r"|(^|[.!?]\s+)(use|take|wear|apply|protect|limit|reduce|keep|give|rest|blink|look|try|eat"
        r"|drink|always|never|see|visit|get)\b",
        re.IGNORECASE,
    ),
    "medicine": re.compile(
        r"\b(drops?|eye ?drops|medicine|medication|medicines|drug|drugs|tablet|tablets|pill|pills"
        r"|capsule|ointment|antibiotic|antibiotics|steroid|steroids|supplement|supplements|vitamin"
        r"|vitamins|omega|lutein|timolol|latanoprost|atropine|artificial tears|lubricant|dose|dosage"
        r"|mg|ml|mcg)\b",
        re.IGNORECASE,
    ),
    "number_unit": re.compile(
        r"\d+(\.\d+)?\s*(%|percent|mg|ml|mcg|mmhg|iu|units?)(?!\w)",
        re.IGNORECASE,
    ),
    "diagnosis_or_risk": re.compile(
        r"\b(disease|diseases|disorder|condition|conditions|infection|infections|diagnos\w*|risk|risks"
        r"|symptom|symptoms|blindness|going blind|damage|danger\w*|harm\w*|cure|cures|treat\w*|prevent\w*"
        r"|glaucoma|cataracts?|macular|degeneration|retinopathy|detachment|conjunctivitis|myopia"
        r"|hyperopia|astigmatism|amblyopia|keratoconus|diabetes|diabetic|cancer|tumou?r|allerg\w*)\b",
        re.IGNORECASE,
    ),
    "url": re.compile(r"https?://|www\.|\.com\b|\.org\b|\.in\b", re.IGNORECASE),
}

CONDITION_WORDS: frozenset[str] = frozenset(
    {
        "glaucoma",
        "cataract",
        "cataracts",
        "conjunctivitis",
        "keratitis",
        "uveitis",
        "iritis",
        "retinopathy",
        "maculopathy",
        "degeneration",
        "detachment",
        "tear",
        "hole",
        "blepharitis",
        "chalazion",
        "stye",
        "keratoconus",
        "amblyopia",
        "strabismus",
        "myopia",
        "hyperopia",
        "presbyopia",
        "astigmatism",
        "infection",
        "ulcer",
        "edema",
        "oedema",
        "haemorrhage",
        "hemorrhage",
        "neuritis",
        "papilledema",
        "occlusion",
        "pterygium",
        "pinguecula",
        "dry eye disease",
        "diabetes",
        "diabetic",
        "hypertension",
        "hypertensive",
        "tumour",
        "tumor",
        "cancer",
        "melanoma",
        "retinoblastoma",
    }
)

# A medicine-like token: capitalised brand-ish words ending in common drug suffixes, or a word
# followed by a dose / dosing frequency.
_DRUG_SUFFIX = re.compile(
    r"\b[a-z]{3,}(olol|prost|mycin|micin|floxacin|oxacin|cillin|zolamide|sone|solone|olone"
    r"|azole|ine|ide|ate|nac|fen|lukast|tadine|stine|cort|mab|vir)\b",
    re.IGNORECASE,
)
_NUMBER_UNIT = re.compile(
    r"(?<![\w./])(\d+(?:[./]\d+)?)\s*(%|mg|ml|mcg|mmhg|mm|iu|drops?|times|hours?|hrs?|days?"
    r"|weeks?|months?|years?)?(?!\w)",
    re.IGNORECASE,
)


def _numbers(text: str) -> dict[str, set[str]]:
    """bare number -> units it appears with in `text` ("" when it has no unit)."""
    out: dict[str, set[str]] = {}
    for m in _NUMBER_UNIT.finditer(text):
        out.setdefault(m.group(1), set()).add((m.group(2) or "").lower().rstrip("s"))
    return out


_WORD = re.compile(r"[a-z]+(?:'[a-z]+)?", re.IGNORECASE)


def check_fact(text: str, keywords: Iterable[str] = ()) -> list[str]:
    """Violation codes for an eye-fact candidate (empty list = safe to show)."""
    violations: list[str] = []
    stripped = text.strip()
    if not stripped:
        return ["empty"]
    if len(stripped.split()) > MAX_FACT_WORDS:
        violations.append("too_long")
    for code, pattern in ADVICE_PATTERNS.items():
        if pattern.search(stripped):
            violations.append(code)
    kw = [k.lower() for k in keywords if k]
    if kw and not any(k in stripped.lower() for k in kw):
        violations.append("off_topic")
    return violations


def _strings(draft: dict[str, Any]) -> list[str]:
    out: list[str] = []
    for key in ("what_we_checked", "next_steps", "unclear_in_notes"):
        out.extend(str(x) for x in draft.get(key) or [])
    for key in ("what_we_found", "follow_up"):
        if draft.get(key):
            out.append(str(draft[key]))
    for med in draft.get("medicines_mentioned") or []:
        out.append(f"{med.get('name', '')} {med.get('instructions_verbatim', '')}")
    return out


def check_summary(draft: dict[str, Any], notes: str) -> list[str]:
    """Faithfulness flags for a visit-summary draft. Each flag is a short human-readable
    string; the doctor must edit or acknowledge each before approval."""
    flags: list[str] = []
    notes_l = notes.lower()
    notes_numbers = _numbers(notes)
    notes_words = {w.lower() for w in _WORD.findall(notes)}

    if not (draft.get("what_we_found") or "").strip():
        flags.append("empty section: what_we_found")
    if not draft.get("what_we_checked"):
        flags.append("empty section: what_we_checked")

    seen: set[str] = set()
    for med in draft.get("medicines_mentioned") or []:
        name = str(med.get("name", "")).strip()
        if name and name.lower() not in notes_l:
            flags.append(f"medicine not in notes: {name}")
            seen.add(name.lower())
        instructions = str(med.get("instructions_verbatim", "")).strip()
        if instructions and instructions.lower() not in notes_l:
            flags.append(f"medicine instructions not verbatim: {name or instructions[:40]}")

    for text in _strings(draft):
        for m in _NUMBER_UNIT.finditer(text):
            number, unit = m.group(1), (m.group(2) or "").lower().rstrip("s")
            key = f"{number}{unit}"
            if key in seen:
                continue
            known_units = notes_numbers.get(number)
            if known_units is None:
                seen.add(key)
                flags.append(f"number not in notes: {m.group(0).strip()}")
            elif unit and (known_units - {""}) and unit not in known_units:
                seen.add(key)
                flags.append(f"unit differs from notes: {m.group(0).strip()}")
        for m in _DRUG_SUFFIX.finditer(text):
            word = m.group(0).lower()
            if word not in notes_words and word not in _COMMON_WORDS and word not in seen:
                seen.add(word)
                flags.append(f"possible medicine not in notes: {m.group(0)}")
        lowered = text.lower()
        for cond in CONDITION_WORDS:
            if (
                re.search(rf"\b{re.escape(cond)}\b", lowered)
                and cond not in notes_l
                and cond not in seen
            ):
                seen.add(cond)
                flags.append(f"condition not in notes: {cond}")
    return flags


# Everyday words that happen to match a drug suffix; never flagged.
_COMMON_WORDS: frozenset[str] = frozenset(
    {
        "examine",
        "routine",
        "determine",
        "machine",
        "medicine",
        "outside",
        "inside",
        "decide",
        "provide",
        "guide",
        "side",
        "wide",
        "aside",
        "beside",
        "slide",
        "separate",
        "accurate",
        "adequate",
        "appropriate",
        "moderate",
        "immediate",
        "estimate",
        "indicate",
        "update",
        "private",
        "climate",
        "rate",
        "state",
        "date",
        "late",
        "plate",
        "create",
        "dilate",
        "relate",
        "normal",
        "baseline",
        "combine",
        "decline",
        "online",
        "define",
        "fine",
        "line",
        "nine",
        "shine",
        "spine",
        "mine",
        "since",
        "pressure",
        "mature",
        "response",
        "reason",
        "person",
        "lesson",
        "season",
        "glide",
        "include",
        "conclude",
        "magnitude",
        "attitude",
        "fortnight",
        "evaluate",
        "intermediate",
        "lubricate",
        "candidate",
        "absolute",
        "contribute",
        "complete",
        "approximate",
        "ultimate",
        "reside",
        "stride",
        "hide",
        "ride",
        "pride",
        "bride",
        "tide",
        "confide",
        "coincide",
        "override",
    }
)


def safe_concern(phrase: str | None) -> str | None:
    """Accept an LLM concern phrase only if it is short, plain, and names no condition."""
    if not phrase:
        return None
    p = phrase.strip().strip(".").lower()
    if not p or len(p) > 60 or len(p.split()) > 6 or not re.fullmatch(r"[a-z][a-z '\-]*", p):
        return None
    words = set(p.replace("-", " ").split())
    if words & {w for w in CONDITION_WORDS if " " not in w}:
        return None
    if ADVICE_PATTERNS["medicine"].search(p) or ADVICE_PATTERNS["diagnosis_or_risk"].search(p):
        return None
    return p
