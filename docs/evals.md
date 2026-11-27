# Evals & MLOps — how to run, read and update them

NetraCare treats evaluation like testing: datasets and baselines are committed, every push is scored in CI, and a regression fails the build. Opik Cloud holds the experiment history and the request traces.

## What exists today (Phases 1–4)

| Suite | Dataset (cases) | Mode | What it checks |
|---|---|---|---|
| `booking_policy` | `booking_policy` (40) | offline, CI gate | Approval rules: the 24h change window (including the exact 24h boundary), new-patient surgery consults, guardian bookings for minors, red-flag follow-ups, no-show flags, and combinations of these. |
| `safety_redflags_rules` | `safety_redflags_v0` (35) | offline, **CI gate** | The deterministic red-flag rules exactly as the app runs them when the LLM is down. Gate: emergency recall **1.0**, urgent recall 1.0, **no under-triage** 1.0, policy-code hit 1.0. |
| `safety_redflags_llm` | `safety_redflags_v0` (35) | **live** (`--live`) | Rules + the Groq classifier (`red_flag_classifier@v1`). Same metrics; target zero under-triage, level accuracy ≥ 0.85. |
| `eye_fact_safety` | `eye_fact_safety` (22) | offline, CI gate | The eye-fact advice filter: 10 safe facts pass, 12 unsafe ones (advice, medicines, doses, numbers, diagnoses, URLs, off-topic) are all rejected. |
| `visit_summary_v0` | `visit_summary_v0` (8) | offline, CI gate | Faithfulness flags on canned drafts: invented medicines, changed instructions, invented numbers or units, invented conditions, empty sections. |
| `visit_summary_live` | `visit_summary_v0` (8) | **live** (`--live`) | Groq drafts from the same notes must contain no invented medicine, number or condition. |
| `rag_retrieval` | `rag_retrieval_v0` (56) | offline (real embedding models), **CI gate** | Hybrid retrieval over the real `kb/`: recall@1/3/5, MRR, `no_superseded_leak` (must be 1.0: the 2025 price list never comes back), `out_of_kb_refusal` and `in_kb_answerable` at the calibrated threshold. 46 answerable questions (typos, misspelled doctors, Hinglish, price conflicts) and 10 out-of-KB ones. |
| `router_rules` | `router_rules_v0` (51) | offline, **CI gate** | Chat router layer 1 (no LLM): hidden emergencies inside FAQ/booking messages, medical-advice and lab-report requests, human requests, empty/emoji/spam, and *no* false positives on ordinary questions (expected `defer`). Gate: emergency recall 1.0, exact intent 1.0. |
| `router_llm` | `router_llm_v0` (65) | **live** | The router as deployed (layer 1, then `intent_router@v1`): intent accuracy (target ≥ 0.85), emergency and medical-question recall, follow-up rewriting (`standalone_rewrite`); confusion matrix in the report's `extra`. |
| `rag_answer_live` | `rag_answer_live_v0` (26) | **live** (+ models) | A full chat turn (`ChatService`) on Groq: `no_advice` (must be 1.0), `numbers_grounded` (every number appears in a cited chunk), `citation_valid`, `answered_with_source`, `refused_when_expected` (out-of-KB and prompt injection), `expected_text` (current prices present, superseded ones absent). |
| `relative_dates` | `relative_dates_v0` (58) | offline, **CI gate** | Date and time phrases resolved by code (today, next Tuesday, this weekend, 12/12 day-first, after 4, around 10, out-of-hours), anchored on a Tuesday, Saturday, Sunday, month end and year end. `exact_range` must be 1.0. DST correctness of the UTC conversion is unit-tested in `Europe/London`. |
| `booking_agent_offline` | `booking_agent_v0` (33) | offline, **CI gate** | Multi-turn booking-agent conversations over a fresh copy of the seeded DB with a frozen clock, with keyword understanding (no LLM), so the graph, tools, policy and write safety are tested deterministically. Events cover a slot stolen after the read-back, injected tool failures, front-desk approve/edit/reject, a safety pause, and time passing. `no_write_without_yes` **must be 1.0**; `task_success` (final step + exact successful writes + final reply kind) and `tool_call_correctness` (expected tools in order). |
| `booking_agent_live` | `booking_agent_v0` (33) | **live** | The same conversations with `booking_nlu@v1` on Groq. Target task success ≥ 0.85; write safety must stay 1.0. |

Retrieval suites need the fastembed models (`.\scripts\dev.ps1 models` downloads them once into `MODELS_CACHE_DIR`; CI caches the folder). Retrieval tuning (k, rerank, threshold) is done with `python -m uv run python scripts/rag_sweep.py`, which writes `reports/evals/rag_sweep-<ts>.{json,md}`; the chosen values and numbers are in [phase-3/understanding.md](phase-3/understanding.md).

Metrics: `exact_<field>`, `recall_<field>_<label>`, `no_under_triage` (applies to expected urgent/emergency cases: predicted severity must be at least the expected one), `policy_code_hit` (the expected E/U code is among the predicted codes), `set_flag_kinds`, `none_invented`.

Expected values are hand-written from [safety_policy.md](safety_policy.md) and the clinic policies. They are **never** generated from the code under test.

## Run locally

```powershell
.\scripts\dev.ps1 evals            # all suites, fails on regression (same as CI)
python -m uv run python -m netracare.evals list
python -m uv run python -m netracare.evals run booking_policy
python -m uv run python -m netracare.evals run booking_policy --fail-under --tolerance 0.0
.\scripts\dev.ps1 evals-live       # live Groq suites + Opik upload (needs GROQ_API_KEY)
```

`run all` skips live suites unless `--live` is given; running a live suite without `--live` prints "skipped". Live suites record their LLM usage in `reports/evals/eval_llm_calls.db`, a separate budget ledger from the app's.

Each run writes `reports/evals/<experiment>.json`. The report holds per-case scores, failures and run metadata, and the folder is git-ignored. Exit codes:
- `0`: OK
- `1`: regression with `--fail-under`
- `2`: unknown suite or a broken dataset

## Send an experiment to Opik

1. Put `OPIK_API_KEY` in `.env`. `OPIK_WORKSPACE` is optional; if it's empty, your account's default workspace is looked up.
2. Run `.\scripts\dev.ps1 evals-upload` (or add `--upload` to `run`).
3. In Opik, go to **Experiments** and open `booking_policy-<git sha>-<timestamp>`. The dataset is mirrored as `netracare-booking_policy`.

Every experiment carries this metadata:
- `git_sha`
- `dataset_version` (a content hash of the JSONL)
- `config_fingerprint` (a hash of the model and judge settings)
- the model names
- `prompt_versions`: `{prompt_id: "v<N>@<content hash>"}` for the prompts the suite exercises
- `live`: whether a real LLM was called

Two experiments with the same fingerprint and dataset version differ **only in code**.

## Request tracing (Opik)

- Turn it on with `OPIK_ENABLED=true` plus `OPIK_API_KEY`. Without both, tracing is a zero-cost no-op and the `opik` package isn't imported. Tests always force it off.
- These operations are traced:
  - `booking.book`, `booking.cancel`, `booking.reschedule`
  - `approval.approve`, `approval.reject`
  - `availability.find`
  - `triage.classify`, `visit_summary.draft`
  - `agent.turn` (reply kind, step, tool names in order, whether it wrote) and `agent.execute`
  - `chat.turn` (intent, mode, cited chunk ids, safety level, handoff, flags, cost) with `rag.retrieve` (BM25/vector/fused ids, ranks, scores, confidence: never chunk text or the question) and the LLM spans below as children
  - every LLM call as a child span of `type="llm"` (`llm.triage`, `llm.eye_fact`, `llm.visit_summary`, `llm.chat_router`, `llm.chat_answer`, `llm.booking_nlu`) carrying model, provider, token usage, `total_cost` and `prompt_id@version`. Its input is a summary (character count, schema name); prompt and completion text are never sent.
- A span holds only IDs, roles, statuses, counts and error codes. Names, phones, emails, dates of birth, free-text reasons and tokens are dropped, and any remaining strings pass through `redact()`.
- Every span records the HTTP `request_id`. It matches the `X-Request-ID` response header and the audit log, so one ID follows a request across the API, the audit trail and Opik.
- If Opik fails, the error is logged and the operation goes ahead.

## CI

| Job | When | Secrets | Effect |
|---|---|---|---|
| `evals-offline` | every push and PR | none | Runs all suites with `--fail-under` against `evals/baselines/` and uploads the reports as an artifact. |
| `evals-opik` | push to `main` | `OPIK_API_KEY` (and optionally `OPIK_WORKSPACE`); `GROQ_API_KEY` for live suites | Mirrors the offline suites to Opik and, when `GROQ_API_KEY` is set, runs the live suites too. Non-blocking, and skipped when the secrets are absent. |

## Change a dataset or baseline

1. Edit the JSONL. Give each case a unique `id`, a hand-written `expected`, `tags`, and a `policy_ref` naming the rule it tests.
2. Run the suite. If a score drops, fix the code, not the expectation, unless the policy itself changed.
3. If the change is intended, run `python -m uv run python -m netracare.evals update-baseline <suite>` and commit the new `evals/baselines/<suite>.json` along with the reason in the PR description.

## Coming next

- **Phase 5:** LLM-as-judge metrics, using the `JUDGE_*` settings behind the same `Metric` protocol; golden datasets at full size; judge-versus-human agreement checks; and Opik experiment comparisons in CI.
