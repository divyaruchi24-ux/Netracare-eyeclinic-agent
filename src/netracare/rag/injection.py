"""Prompt-injection detector shared by KB ingestion and chat (safety policy section 9).

In KB documents a hit refuses the chunk (unless the doc is explicitly trusted). In chat a hit
only logs `prompt_injection_suspected`: the conversation continues normally, because the
prompts already treat user text as data.
"""

from __future__ import annotations

import re

INJECTION_PATTERNS: dict[str, re.Pattern[str]] = {
    "ignore_instructions": re.compile(
        r"\b(ignore|disregard|forget|override)\b.{0,30}\b(previous|prior|above|earlier|all|your|the)\b"
        r".{0,20}\b(instructions?|rules?|prompts?|polic(y|ies)|guidelines?)\b",
        re.IGNORECASE,
    ),
    "role_change": re.compile(
        r"\byou are now\b|\bact as (a|an|the)?\s*(doctor|admin|developer|system)\b"
        r"|\bpretend (to be|you are)\b|\bfrom now on,? you\b",
        re.IGNORECASE,
    ),
    "system_prompt": re.compile(
        r"\b(system|developer) (prompt|message|instructions?)\b"
        r"|\breveal (your|the) (prompt|instructions)\b",
        re.IGNORECASE,
    ),
    "data_exfiltration": re.compile(
        r"\b(list|show|print|dump|give me|export)\b.{0,20}\b(all|every|other)\b.{0,20}"
        r"\b(patients?|users?|records?|appointments?|emails?|phone numbers?)\b",
        re.IGNORECASE,
    ),
    "tool_markup": re.compile(r"<\s*/?\s*(system|assistant|tool|instructions?)\s*>", re.IGNORECASE),
}


def scan_text(text: str) -> list[str]:
    """Names of the injection patterns found in `text` (empty when clean)."""
    return [name for name, pattern in INJECTION_PATTERNS.items() if pattern.search(text)]
