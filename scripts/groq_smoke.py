"""Live Groq smoke test: `python -m uv run python scripts/groq_smoke.py` (needs GROQ_API_KEY).

Checks, against the real API and the configured database:
1. both configured models are listed by Groq;
2. structured triage output (strict json_schema) round-trips on 3 fixed texts (fast model);
3. one eye-fact is generated and passes the advice filter (fast model);
4. one visit-summary draft round-trips (reasoning model);
5. every call wrote an `llm_calls` row with tokens and cost; Opik spans are flushed if enabled.
Exit code 0 on success, 1 on any failed check.
"""

from __future__ import annotations

import sys
import time

from sqlalchemy import func, select

from netracare.config import get_settings
from netracare.db.models import LLMCall
from netracare.db.session import make_engine, make_session_factory
from netracare.domain.clock import SystemClock
from netracare.guardrails.advice_filter import check_fact, check_summary
from netracare.guardrails.redflag_rules import detect
from netracare.llm.client import LLMClient
from netracare.llm.pricing import Pricing
from netracare.llm.prompts import PromptRegistry
from netracare.llm.providers import GroqProvider
from netracare.llm.schemas import EyeFactOutput, VisitSummaryDraft
from netracare.observability.tracing import configure_tracing, flush_tracing
from netracare.services.eye_fact_service import load_topics
from netracare.services.triage_service import build_chain, decide

TEXTS = [
    ("lol got bleach in my eye, anyway can I book tuesday", "emergency"),
    ("a few new floaters, no flashes", "urgent"),
    ("my eyes itch a bit in the evenings", "routine"),
]
NOTES = (
    "Routine review. VA 6/6 both eyes. IOP 15 mmHg OU. Mild dry eye. Start "
    "carboxymethylcellulose 0.5% 1 drop 4 times daily both eyes. Review in 3 months."
)


def main() -> int:
    settings = get_settings()
    key = settings.groq_api_key.get_secret_value() if settings.groq_api_key else ""
    if not key:
        print("GROQ_API_KEY is not set in .env - nothing to test.")
        return 1
    tracing = configure_tracing(settings)
    engine = make_engine(settings.database_url)
    factory = make_session_factory(engine)
    provider = GroqProvider(key)
    llm = LLMClient(
        settings=settings,
        clock=SystemClock(),
        provider=provider,
        registry=PromptRegistry(settings.prompts_dir).load(),
        pricing=Pricing.load(settings.pricing_file),
        session_factory=factory,
    )
    ok = True

    def check(cond: bool, label: str) -> None:
        nonlocal ok
        ok &= cond
        print(f"  [{'ok' if cond else 'FAIL'}] {label}")

    with factory() as s:
        before = s.scalar(select(func.count()).select_from(LLMCall)) or 0

    print("1. models")
    models = set(provider.list_models())
    for m in (settings.groq_model_fast, settings.groq_model_reasoning):
        check(m in models, f"{m} available")

    print("2. structured triage (fast model)")
    for text, expected in TEXTS:
        t0 = time.perf_counter()
        out = build_chain(llm, actor_user_id=None, names=[]).invoke(text)
        d = decide(text, detect(text), out, "llm+rules", settings.triage_confidence_threshold)
        ms = int((time.perf_counter() - t0) * 1000)
        levels = f"llm={out.level.value:12} final={d.level.value:12}"
        print(f"     {text[:45]!r:50} {levels} conf={out.confidence:.2f} {ms}ms")
        check(d.level.value == expected, f"final level {expected}")

    print("3. eye fact (fast model)")
    topics, _ = load_topics(settings.prompts_dir)
    topic = topics[0]
    fact = llm.generate(
        "eye_fact",
        {"topic_id": topic.id, "topic_title": topic.title, "keywords": ", ".join(topic.keywords)},
        purpose="smoke",
        schema=EyeFactOutput,
    )
    violations = check_fact(fact.value.fact, topic.keywords)
    print(f"     {fact.value.fact}")
    print(f"     tokens {fact.input_tokens}/{fact.output_tokens} cost ${fact.cost_usd}")
    check(not violations, f"advice filter passed {violations or ''}")

    print("4. visit summary (reasoning model)")
    draft = llm.generate(
        "visit_summary", {"notes": NOTES}, purpose="smoke", schema=VisitSummaryDraft
    )
    flags = check_summary(draft.value.model_dump(), NOTES)
    print(f"     found: {draft.value.what_we_found}")
    usage = f"tokens {draft.input_tokens}/{draft.output_tokens} cost ${draft.cost_usd}"
    print(f"     model {draft.model} {usage} {draft.latency_ms}ms attempts {draft.attempts}")
    check(isinstance(draft.value, VisitSummaryDraft), "draft validated")
    print(f"     faithfulness flags: {flags or 'none'}")

    print("5. usage rows")
    with factory() as s:
        after = s.scalar(select(func.count()).select_from(LLMCall)) or 0
        last = s.scalars(select(LLMCall).order_by(LLMCall.at_utc.desc()).limit(1)).one()
    check(after - before >= 5, f"{after - before} llm_calls rows written")
    check(last.output_tokens > 0 and last.cost_usd > 0, "tokens and cost recorded")
    if tracing:
        flush_tracing()
        print("  [ok] Opik spans flushed")
    engine.dispose()
    print("PASS" if ok else "FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
