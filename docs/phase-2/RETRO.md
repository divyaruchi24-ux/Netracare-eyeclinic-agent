# Phase 2 — Retrospective (first LLM features)

**Dates:** 2026-11-18 (planning) – 2026-11-19 (execution) · **Commit:** `phase-2: first LLM features - triage, eye-fact, visit summaries, LLM client`

## What shipped
- **One LLM layer** (`netracare/llm/`): `PromptRegistry` (versioned YAML prompts, content hashes, LangChain `ChatPromptTemplate` rendering), `GroqProvider` / `FakeProvider` / `DisabledProvider`, and `LLMClient`, which does PII redaction, a budget check before every call ($1.50/day, $20/month from `llm_calls`), tenacity retries (only 429/5xx/timeouts, honouring `retry-after`), strict `json_schema` structured output with one repair round-trip, cost from `config/model_pricing.yaml`, a usage row in its own transaction, and an Opik `type="llm"` span with usage and cost but no text.
- **Guardrails** (`netracare/guardrails/`): the §5/§6/§10 scripts as constants (a test diffs them against `safety_policy.md`), deterministic red-flag rules for E1–E10/U1–U6 (negation within a clause, typos, child words, hidden emergencies), an advice filter for eye facts and faithfulness checks for summaries, and `safe_concern` for LLM routing phrases.
- **Red-flag triage**: rules floor + LLM (can only escalate) + low-confidence escalation; verbatim scripts; front-desk alerts with redacted excerpts (ack/resolve, optimistic lock); a patient booking with an emergency reason is paused (409 `emergency_detected`), an urgent one needs approval; staff bookings record the level. `POST /triage` is public and rate-limited.
- **Eye-fact banner**: deterministic daily topic from 20 approved topics, generated once per day, filtered, static fallback; on every page.
- **Visit-summary drafts**: doctor-only drafting with the patient's name/phone/email redacted, faithfulness flags that block approval until edited or acknowledged, patient 404 until approved, manual path when the LLM is down, audit + `visit_summary_ready` notification.
- **Admin AI usage** (`GET /admin/llm-usage` + page) and four new UI pages (Symptom check, Alerts, Visit notes, AI usage) plus the banner and approved summaries on My appointments.
- **Evals**: `safety_redflags_rules`, `eye_fact_safety` and `visit_summary_v0` gate CI offline; `safety_redflags_llm` and `visit_summary_live` run live with `--live` and log Opik experiments with `prompt_versions`.
- `scripts/groq_smoke.py`, opt-in `tests/live/` (`RUN_LIVE=1`), and `dev.ps1 groq-smoke` / `evals-live`.

## Verification
| Check | Result |
|---|---|
| Tests | 697 passed (all offline; live tests skipped by default) |
| Coverage | 97% overall; `services/` 97%; `llm/` 99% |
| ruff / ruff format / mypy strict | clean |
| Clean venv `pip install -r requirements.txt -r requirements-dev.txt` | OK: `pip check` clean; groq 1.7.0, langchain-core 1.8.3, tenacity 9.2.1 import |
| Groq smoke test (live) | PASS: both models listed; strict json_schema round-trips on 20b and 120b, first attempt; eye fact passed the filter; 5 `llm_calls` rows with tokens and cost (~$0.0007 total); Opik spans flushed |
| `safety_redflags_rules` (offline gate) | emergency recall 1.0, urgent recall 1.0, no under-triage 1.0, policy-code hit 1.0, level 0.969, route 0.969 |
| `safety_redflags_llm` (live, Opik experiment) | emergency recall 1.0, urgent recall 1.0, **no under-triage 1.0**, level 0.938, route 0.938 (target ≥ 0.85) |
| `eye_fact_safety` / `visit_summary_v0` | 1.0 / 1.0 (all 12 unsafe facts rejected) |
| `visit_summary_live` (live) | 8/8 drafts with no invented medicine, number or condition |
| Live demo against the dev API (port 8001, real Groq) | eye fact generated (source `llm`); bleach message → verbatim script + rinse line + alert; front desk acknowledged it; floaters → urgent, Retina, Dr. Vikram Shetty; patient booking "sudden curtain…" → 409 `emergency_detected`; walk-in → check-in → Groq draft (no flags, no patient name) → patient 404 → approve → patient 200 + notification; admin usage showed every call |

## What went well
- The rules floor made safety independent of Groq, the budget and the model's mood: the CI gate needs no secrets, and the live run confirmed the LLM never lowered a level.
- Writing scripts as constants with a policy-diff test caught two parsing subtleties immediately (the §5.3 placeholder prose and a trailing note in §10).
- Running the live eval early paid off: it found rf-030 (a prompt-injected dose question rated `non_clinical`), which led to the clinical-routine floor and a deterministic medical-question detector.
- `FakeProvider` + `use_llm` kept 100% of the suite offline and fast; the Groq SDK's `include_reasoning=False` meant reasoning text never crossed the wire.

## What was hard / surprises
- **SQLite write lock vs slow LLM calls.** Phase 1's `BEGIN IMMEDIATE` would have held the lock through every model call. Fixed with `outside_transaction` (commit the read transaction, call, reopen a write transaction) and separate short transactions for usage rows.
- **Shell quoting.** Python heredocs turned `\b` and `\n` inside regexes and string literals into control characters three times; regex files are now edited with the Edit tool only.
- **Negation scope.** "no flashes, just floaters" first produced no flag at all; negation now stops at clause boundaries.
- Groq's marketing pricing page has no prices; the model docs do.

## Carry-overs
- Alert de-duplication for retried emergency bookings (same idempotency key → second alert).
- Level accuracy on routine vs non-clinical booking chatter (rf-026) — Phase 3's intent router owns this.
- `qwen/qwen3.8-27b` (judge) is a Groq Preview model; re-check availability before Phase 5.
- Optional: `python -m netracare.llm.prompts sync-opik` was implemented but not exercised live.
- The Phase 1 carry-overs (Postgres, shared rate-limit store) remain for Phase 7.

## Metrics snapshot
- Prompts: `red_flag_classifier@v1`, `eye_fact@v1`, `visit_summary@v1`, `json_repair@v1`.
- Live Groq spend during execution (smoke test, 4 live eval runs, demo): about $0.02 in total (150 calls).
- The first live uploads re-ran tasks inside `opik.evaluate` and hit Groq rate limits; the upload now reuses the scored outputs (see the Decisions Log).
