"""Live Groq smoke test: `python -m uv run python scripts/groq_smoke.py` (needs GROQ_API_KEY).

Checks, against the real API and the configured database:
1. both configured models are listed by Groq;
2. structured triage output (strict json_schema) round-trips on 3 fixed texts (fast model);
3. one eye-fact is generated and passes the advice filter (fast model);
4. one visit-summary draft round-trips (reasoning model);
5. one chat turn: router (fast) -> hybrid retrieval over the ingested KB -> cited answer
   (reasoning) that passes the post-checks; needs `dev.ps1 ingest` (or reseed) first;
6. one booking-agent turn: booking_nlu (fast) -> code-resolved date -> 3 options, no write;
7. every call wrote an `llm_calls` row with tokens and cost; Opik spans are flushed if enabled.
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
    # Model text can contain characters a Windows console cannot encode (e.g. U+202F).
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
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

    print("5. chat turn (router + retrieval + cited answer)")
    from netracare.agents.router import Router
    from netracare.rag.answer import AnswerGenerator
    from netracare.rag.ingest import build_components
    from netracare.rag.retriever import RetrievalFilters, RetrievalIndex

    embedder, store = build_components(settings)
    index = RetrievalIndex(factory, embedder, store, None, settings).load()
    check(index.vectors_ok, f"vector index in sync ({len(index)} chunks)")
    route = Router(llm, index.doctors).route("how much is lasic?")
    print(
        f"     router: {route.intent.value} conf={route.confidence:.2f}"
        f" -> {route.standalone_query!r}"
    )
    check(route.intent.value == "pricing", "router intent pricing")
    retrieval = index.retrieve(
        route.standalone_query, RetrievalFilters(boost_doc_types=("price_list",))
    )
    print(
        f"     retrieval: confidence {retrieval.confidence:.3f}"
        f" ({retrieval.mode}, {retrieval.latency_ms}ms)"
    )
    check(retrieval.confidence >= settings.rag_min_confidence, "above the answer threshold")
    answer = AnswerGenerator(llm, purpose="smoke").generate(route.standalone_query, retrieval)
    print(f"     answer ({answer.mode}): {answer.text}")
    check(answer.mode == "llm" and bool(answer.citations), "cited LLM answer passed post-checks")
    check("52,000" in answer.text and "45,000" not in answer.text, "current LASIK price only")

    print("6. booking agent turn (booking_nlu on a scratch copy of the demo DB, no write)")
    from netracare.evals.booking_tasks import run_conversation

    conv = run_conversation(
        {"turns": ["book a check-up with Dr Kavya Nair next Tuesday morning"], "patient": "P010"},
        llm=llm,
    )
    print(f"     agent: {conv['final_kind']} -> {conv['last_reply'][:120]}")
    check(conv["final_kind"] == "options", "agent proposed options from the LLM's understanding")
    check(conv["writes_ok"] == [], "nothing written without a yes")

    print("7. usage rows")
    with factory() as s:
        after = s.scalar(select(func.count()).select_from(LLMCall)) or 0
        last = s.scalars(select(LLMCall).order_by(LLMCall.at_utc.desc()).limit(1)).one()
    check(after - before >= 8, f"{after - before} llm_calls rows written")
    check(last.output_tokens > 0 and last.cost_usd > 0, "tokens and cost recorded")
    if tracing:
        flush_tracing()
        print("  [ok] Opik spans flushed")
    engine.dispose()
    print("PASS" if ok else "FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
