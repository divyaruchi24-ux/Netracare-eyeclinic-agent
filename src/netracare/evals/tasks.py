"""Eval tasks: functions from a case's `input` to an output dict.

* `booking_policy` - the real approval rules (`domain.policies`).
* `safety_redflags_rules` - the deterministic red-flag layer exactly as the app runs it when
  the LLM is unavailable (CI gate: emergency recall 1.0, zero under-triage).
* `safety_redflags_llm` - rules + the live Groq classifier (`--live` only).
* `eye_fact_safety` - the eye-fact advice filter on adversarial candidates.
* `visit_summary_v0` - faithfulness flags on canned drafts (offline); `visit_summary_live`
  drafts with Groq and expects no invented content (`--live` only).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any

from netracare.domain.enums import AppointmentType, RedFlagLevel
from netracare.domain.policies import BookingContext, requires_approval, should_flag_for_no_shows
from netracare.evals.metrics import (
    CodeHit,
    ExactMatch,
    LabelRecall,
    Metric,
    NoneFlagged,
    SetEquality,
    UnderTriage,
)

Task = Callable[[dict[str, Any]], dict[str, Any]]
_EVAL_NOW = datetime(2026, 11, 10, 3, 30, tzinfo=UTC)
TRIAGE_THRESHOLD = 0.6


def booking_policy_task(inp: dict[str, Any]) -> dict[str, Any]:
    level = inp.get("red_flag_level")
    ctx = BookingContext(
        appt_start_utc=_EVAL_NOW + timedelta(hours=float(inp["hours_until_start"])),
        now_utc=_EVAL_NOW,
        appointment_type=AppointmentType(inp["appointment_type"]),
        is_change=bool(inp["is_change"]),
        is_new_patient=bool(inp["is_new_patient"]),
        patient_is_minor=bool(inp["patient_is_minor"]),
        booked_by_guardian=bool(inp["booked_by_guardian"]),
        red_flag_level=RedFlagLevel(level) if level else None,
        patient_no_show_flagged=should_flag_for_no_shows(int(inp["no_show_count"])),
    )
    reasons = sorted(r.value for r in requires_approval(ctx))
    return {"reasons": reasons, "auto_confirm": not reasons}


# -- triage -------------------------------------------------------------------------------------


def _triage_output(text: str, llm_out: Any, source: str) -> dict[str, Any]:
    from netracare.guardrails.redflag_rules import detect
    from netracare.services.triage_service import SHORTCIRCUIT, decide, is_shortcircuit

    d = (
        SHORTCIRCUIT
        if is_shortcircuit(text)
        else decide(text, detect(text), llm_out, source, TRIAGE_THRESHOLD)
    )
    return {
        "level": d.level.value,
        "route": d.route,
        "codes": d.codes,
        "specialty": d.specialty,
        "source": d.source,
    }


def triage_rules_task(inp: dict[str, Any]) -> dict[str, Any]:
    return _triage_output(str(inp["message"]), None, "rules")


@lru_cache(maxsize=1)
def live_llm() -> Any:  # pragma: no cover - needs GROQ_API_KEY
    """A real LLMClient for live evals. Usage rows go to a dedicated eval DB so eval spend is
    budgeted and auditable separately from the app."""
    from netracare.config import get_settings
    from netracare.db.base import Base
    from netracare.db.session import make_engine, make_session_factory
    from netracare.domain.clock import SystemClock
    from netracare.llm.client import LLMClient, make_provider
    from netracare.llm.pricing import Pricing
    from netracare.llm.prompts import PromptRegistry
    from netracare.llm.providers import DisabledProvider

    settings = get_settings()
    provider = make_provider(settings)
    if isinstance(provider, DisabledProvider):
        raise RuntimeError("live evals need GROQ_API_KEY and LLM_ENABLED=true")
    path = Path("reports/evals/eval_llm_calls.db")
    path.parent.mkdir(parents=True, exist_ok=True)
    engine = make_engine(f"sqlite:///{path.as_posix()}")
    Base.metadata.create_all(engine)
    return LLMClient(
        settings=settings,
        clock=SystemClock(),
        provider=provider,
        registry=PromptRegistry(settings.prompts_dir).load(),
        pricing=Pricing.load(settings.pricing_file),
        session_factory=make_session_factory(engine),
    )


def triage_llm_task(inp: dict[str, Any]) -> dict[str, Any]:  # pragma: no cover - live
    from netracare.llm.types import LLMError
    from netracare.services.triage_service import build_chain, is_shortcircuit

    text = str(inp["message"])
    if is_shortcircuit(text):
        return _triage_output(text, None, "shortcircuit")
    try:
        out = build_chain(live_llm(), actor_user_id=None, names=[]).invoke(text)
        return _triage_output(text, out, "llm+rules")
    except LLMError as exc:
        result = _triage_output(text, None, "rules_fallback")
        result["llm_error"] = exc.code
        return result


# -- eye fact ----------------------------------------------------------------------------------


@lru_cache(maxsize=1)
def _topic_keywords() -> dict[str, tuple[str, ...]]:
    from netracare.config import get_settings
    from netracare.services.eye_fact_service import load_topics

    topics, _ = load_topics(get_settings().prompts_dir)
    return {t.id: t.keywords for t in topics}


def eye_fact_safety_task(inp: dict[str, Any]) -> dict[str, Any]:
    from netracare.guardrails.advice_filter import check_fact

    violations = check_fact(str(inp["text"]), _topic_keywords().get(str(inp["topic_id"]), ()))
    safe = not violations
    return {"safe": safe, "safe_label": "safe" if safe else "unsafe", "violations": violations}


# -- visit summary -----------------------------------------------------------------------------


def _flag_kinds(flags: list[str]) -> list[str]:
    return sorted({f.split(":", 1)[0] for f in flags})


def visit_summary_offline_task(inp: dict[str, Any]) -> dict[str, Any]:
    from netracare.guardrails.advice_filter import check_summary

    flags = check_summary(inp["draft"], str(inp["notes"]))
    return {"flag_kinds": _flag_kinds(flags), "flags": flags}


_INVENTION_KINDS = ("not in notes", "not verbatim", "differs from notes")


def visit_summary_live_task(inp: dict[str, Any]) -> dict[str, Any]:  # pragma: no cover - live
    from netracare.guardrails.advice_filter import check_summary
    from netracare.llm.schemas import VisitSummaryDraft

    result = live_llm().generate(
        "visit_summary", {"notes": inp["notes"]}, purpose="eval", schema=VisitSummaryDraft
    )
    flags = check_summary(result.value.model_dump(), str(inp["notes"]))
    invented = [f for f in flags if any(k in f for k in _INVENTION_KINDS)]
    return {"invented": invented, "flags": flags}


@dataclass(frozen=True)
class EvalSuite:
    dataset: str
    task: Task | None  # None => dataset is schema-validated only (task lands in a later phase)
    metrics: list[Metric] = field(default_factory=list)
    description: str = ""
    requires_live: bool = False  # calls a real LLM: only with `--live`
    prompts: tuple[str, ...] = ()  # prompt ids whose versions go in the run metadata


_TRIAGE_METRICS: list[Metric] = [
    ExactMatch("level"),
    ExactMatch("route"),
    LabelRecall("level", "emergency"),
    LabelRecall("level", "urgent"),
    UnderTriage(),
    CodeHit(),
]

SUITES: dict[str, EvalSuite] = {
    "booking_policy": EvalSuite(
        dataset="booking_policy",
        task=booking_policy_task,
        metrics=[SetEquality("reasons"), ExactMatch("auto_confirm")],
        description="Approval rules: 24h window, surgery consult, minors, red flags, no-shows",
    ),
    "safety_redflags_rules": EvalSuite(
        dataset="safety_redflags_v0",
        task=triage_rules_task,
        metrics=_TRIAGE_METRICS,
        description="Deterministic red-flag rules (LLM-free floor); gate: emergency recall 1.0",
    ),
    "safety_redflags_llm": EvalSuite(
        dataset="safety_redflags_v0",
        task=triage_llm_task,
        metrics=_TRIAGE_METRICS,
        description="Rules + live Groq classifier; target: zero under-triage",
        requires_live=True,
        prompts=("red_flag_classifier", "json_repair"),
    ),
    "eye_fact_safety": EvalSuite(
        dataset="eye_fact_safety",
        task=eye_fact_safety_task,
        metrics=[ExactMatch("safe"), LabelRecall("safe_label", "unsafe")],
        description="Advice filter rejects every unsafe eye-fact candidate",
    ),
    "visit_summary_v0": EvalSuite(
        dataset="visit_summary_v0",
        task=visit_summary_offline_task,
        metrics=[SetEquality("flag_kinds")],
        description="Faithfulness flags on canned drafts (invented medicines, numbers, conditions)",
    ),
    "visit_summary_live": EvalSuite(
        dataset="visit_summary_v0",
        task=visit_summary_live_task,
        metrics=[NoneFlagged("invented")],
        description="Live Groq drafts from the same notes: no invented content",
        requires_live=True,
        prompts=("visit_summary", "json_repair"),
    ),
}
