"""Structured logging (JSON via structlog) with request-id context and basic PII redaction."""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable, MutableMapping
from contextvars import ContextVar
from typing import Any

import structlog

request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_PHONE_RE = re.compile(r"(?<![\d-])(?:\+?\d[\d\s-]{8,}\d)(?![\d-])")
_ISO_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
_MIN_PHONE_DIGITS = 10


def _mask_phone(match: re.Match[str]) -> str:
    text = match.group(0)
    if _ISO_DATE_RE.fullmatch(text.strip()):
        return text
    digits = sum(ch.isdigit() for ch in text)
    return "[PHONE]" if digits >= _MIN_PHONE_DIGITS else text


def redact(value: str) -> str:
    """Mask emails and phone numbers (names: see `redact_names`)."""
    value = _EMAIL_RE.sub("[EMAIL]", value)
    return _PHONE_RE.sub(_mask_phone, value)


def redact_names(value: str, names: Iterable[str], replacement: str = "the patient") -> str:
    """Replace each known name (and each of its words of >= 3 letters) case-insensitively on
    word boundaries. Longest first, so "Asha Verma" is replaced before "Asha"."""
    parts: set[str] = set()
    for name in names:
        name = (name or "").strip()
        if not name:
            continue
        parts.add(name)
        parts.update(w for w in re.split(r"\s+", name) if len(w) >= 3 and w.isalpha())
    for part in sorted(parts, key=len, reverse=True):
        value = re.sub(rf"(?<!\w){re.escape(part)}(?!\w)", replacement, value, flags=re.IGNORECASE)
    return value


def redact_all(value: str, names: Iterable[str] = ()) -> str:
    """Emails, phone numbers and the given names."""
    masked = redact(value)  # emails/phones first, so names inside them do not break the match
    return redact_names(masked, names) if names else masked


def _add_request_id(
    _: Any, __: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    rid = request_id_var.get()
    if rid is not None:
        event_dict["request_id"] = rid
    return event_dict


def _redact_strings(_: Any, __: str, event_dict: MutableMapping[str, Any]) -> dict[str, Any]:
    return {k: redact(v) if isinstance(v, str) else v for k, v in event_dict.items()}


def configure_logging(level: str = "INFO") -> None:
    logging.basicConfig(format="%(message)s", level=level.upper())
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            _add_request_id,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            _redact_strings,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.getLevelName(level.upper())),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str | None = None) -> Any:
    return structlog.get_logger(name)
