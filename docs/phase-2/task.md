# Phase 2: Tasks (exact changes)

Paths are relative to the repo root. Verify each group with `.\scripts\dev.ps1 check`. Every unit and API test uses `FakeProvider`. Tests that hit Groq are marked `live` and skipped unless `RUN_LIVE=1`.

## T0. Planning
- [x] `docs/phase-2/understanding.md`, `plan.md`, `task.md`
- [x] User approval: "Approve Phase 2 plan?"

## T1. Foundations
- [x] `uv add groq langchain-core tenacity`.
- [x] `config.py`: add these settings and update `.env.example` to match:
  - `llm_enabled: bool = True`
  - `llm_timeout_fast_s: float = 15`
  - `llm_timeout_reasoning_s: float = 45`
  - `llm_max_retries: int = 3`
  - `daily_llm_budget_usd: float = 1.5`
  - `triage_confidence_threshold: float = 0.6`
  - `triage_rate_limit_per_min: int = 10`
  - `prompts_dir: str = "prompts"`
  - `pricing_file: str = "config/model_pricing.yaml"`
- [x] `config/model_pricing.yaml`: `verified_on`, `source_url`, and `models: {openai/gpt-oss-120b: {input_per_m, output_per_m}, openai/gpt-oss-20b: {...}, qwen/qwen3.8-27b: {...}}`. Verify the values against Groq's pricing page before filling them in.
- [x] `src/netracare/domain/enums.py`: add
  - `TriageLevel` (alias of `RedFlagLevel`, with an order helper `severity()`)
  - `AlertStatus(open, acknowledged, resolved)`
  - `SummaryStatus(draft, approved, discarded)`
  - `LLMCallStatus(ok, error, budget_blocked, invalid_output)`

## T2. Migration 0003
- [x] `db/models.py`:
  - **`LLMCall`**: `id`, `at_utc` (index), `request_id`, `purpose`, `prompt_id`, `prompt_version`, `prompt_hash`, `model`, `input_tokens`, `output_tokens`, `cost_usd` (Numeric 12,6), `latency_ms`, `attempts`, `status`, `error_code?`, `actor_user_id?`
  - **`TriageAlert`**: `id`, `created_at`, `level`, `codes` JSON, `confidence`, `source`, `excerpt_redacted` (≤200), `patient_id?` FK, `actor_user_id?`, `status`, `acknowledged_by?`, `acknowledged_at?`, `resolved_by?`, `resolved_at?`, `resolution_note?`, `version` (version_id_col); index `(status, created_at)`
  - **`EyeFact`**: `day` (PK, Date), `topic`, `text`, `source` (llm|static), `prompt_version?`, `rejected_reason?`, `created_at`
  - **`VisitSummary`**: `id`, `appointment_id` (FK, unique), `doctor_id` FK, `raw_notes` Text, `draft_json` JSON, `final_json` JSON?, `status`, `needs_attention` JSON, `acknowledged_flags` JSON, `prompt_id`, `prompt_version`, `model?`, timestamps, `approved_by?`, `approved_at?`, `version` (version_id_col)
- [x] `alembic revision --autogenerate -m "llm features" --rev-id 0003`, reviewed by hand. The existing drift test covers it.
- [x] `tests/db/test_constraints.py::test_one_summary_per_appointment`.

## T3. Prompt registry
- [x] `src/netracare/llm/prompts.py`:
  - `PromptSpec` (pydantic): `id`, `version`, `tier`, `temperature`, `max_tokens`, `reasoning_effort`, `output_schema`, `system`, `user`, `input_variables`, `changelog`, `owner`
  - `PromptRegistry(root: Path)`: `load()`, `get(id, version=None) -> LoadedPrompt` (spec + `content_hash`), `render(id, variables, version=None) -> list[ChatMessage]` (via `ChatPromptTemplate`), `all()`
  - Validation: unique `(id, version)`; declared variables equal the template variables; the schema name is registered in `netracare.llm.schemas.SCHEMAS`.
  - CLI `python -m netracare.llm.prompts {list,show,sync-opik}`.
- [x] `src/netracare/llm/schemas.py`: `TriageLLMOutput`, `EyeFactOutput(fact: str, topic: str)`, `VisitSummaryDraft`, `MedicineMention`, and `SCHEMAS: dict[str, type[BaseModel]]`.
- [x] Prompts:
  - `prompts/red_flag_classifier/v1.yaml`: data-only delimiters, codes E1–U6 with short definitions, JSON output, no diagnosis
  - `prompts/eye_fact/v1.yaml`
  - `prompts/visit_summary/v1.yaml`: copy medicines verbatim; never add facts; use plain language; list unclear items
  - `prompts/json_repair/v1.yaml`
- [x] Tests `tests/unit/llm/test_prompts.py`: all prompts load; duplicate version rejected; variable mismatch rejected; unknown schema rejected; render escapes braces in user text; hash changes when the file changes; latest-version resolution.

## T4. LLM layer
- [x] `src/netracare/llm/types.py`:
  - `ChatMessage(role, content)`
  - `LLMRequest(model, messages, temperature, max_tokens, reasoning_effort, json_schema: dict | None, timeout_s)`
  - `ProviderResponse(text, input_tokens, output_tokens, model, finish_reason)`
  - `LLMResult[T](value: T | str, call_id, model, cost_usd, latency_ms, prompt_id, prompt_version, attempts)`
  - Errors: `LLMError` (base) with subclasses `LLMUnavailable` (retryable exhausted/timeout/conn), `BudgetExceeded`, `InvalidOutput`, `LLMDisabled`
- [x] `src/netracare/llm/providers.py`:
  - `LLMProvider` Protocol
  - `GroqProvider(api_key, base_url=None)`: maps request to `client.chat.completions.create(...)`, using `response_format` `json_schema` with `strict`, falling back to `json_object` on a 400 about the schema. Drops the `reasoning` field and maps groq exceptions (RateLimitError, APITimeoutError, APIConnectionError, InternalServerError, BadRequestError) to our errors, carrying `retry_after`.
  - `FakeProvider(script: list[ProviderResponse | Exception] | Callable[[LLMRequest], ProviderResponse])`: records `requests`.
- [x] `src/netracare/llm/pricing.py`: `Pricing.load(path)`, `cost(model, in_tokens, out_tokens) -> Decimal` (an unknown model logs a warning and returns 0).
- [x] `src/netracare/llm/budget.py`: `BudgetGuard(session, clock, settings).check(estimated_usd)` sums `llm_calls.cost_usd` for the clinic-local day and month and raises `BudgetExceeded("daily"|"monthly")`.
- [x] `src/netracare/llm/client.py`, `LLMClient(session, settings, clock, provider, registry, pricing)`:
  - `generate(prompt_id, variables, *, purpose, schema=None, actor=None, redact_names: list[str] = ()) -> LLMResult`
  - Steps: disabled check → render → redact (emails, phones, names) → budget → tenacity retry (exponential plus jitter, honouring `retry_after`, max `llm_max_retries`) around `provider.complete` with timeout → parse and validate (one repair call through `json_repair/v1` on failure) → cost → `llm_calls` row (written in a **separate short transaction** so failures are recorded even when the request rolls back) → Opik LLM span (summary input/output, usage, `total_cost`, model, provider) → result.
  - `client_for(settings, session, clock)` factory: `FakeProvider` when `llm_enabled` is false or there's no key; dependency `get_llm_client` (tests override it).
- [x] `observability/tracing.py`: add `llm_span(name, *, model, prompt, purpose) -> context manager` with `set_usage(in, out, cost)`; no-op when disabled; same PII rules.
- [x] Tests `tests/unit/llm/test_client.py`:
  - `test_success_records_call_and_cost`
  - `test_retries_on_429_then_succeeds`
  - `test_gives_up_after_max_retries_raises_unavailable`
  - `test_timeout_is_retryable`
  - `test_no_retry_on_bad_request`
  - `test_budget_daily_blocks_before_provider_call`
  - `test_budget_monthly_blocks`
  - `test_invalid_json_repaired_once`
  - `test_invalid_after_repair_raises`
  - `test_reasoning_text_dropped`
  - `test_pii_redacted_in_prompt_sent`
  - `test_llm_call_row_has_no_prompt_text`
  - `test_failed_call_row_survives_rollback`
  - `test_disabled_raises_llm_disabled`
- [x] `tests/unit/llm/test_groq_provider.py`: request mapping and error mapping with a stubbed `groq.Groq` client; json_schema fallback path.
- [x] `tests/unit/llm/test_pricing_budget.py`.

## T5. Guardrails
- [x] `src/netracare/guardrails/scripts.py`: `EMERGENCY_SCRIPT`, `CHEMICAL_RINSE_LINE`, `URGENT_SCRIPT`, `MEDICAL_REFUSAL`, `LOW_CONFIDENCE`, `ROUTING_TEMPLATES`, `DISCLAIMERS`, `emergency_message(codes)`, `routing_sentence(specialty_code, concern_phrase, doctor_name)`.
- [x] `src/netracare/guardrails/redflag_rules.py`:
  - `RuleMatch(code, level, span)`
  - `detect(text) -> list[RuleMatch]`, with patterns for E1–E10 and U1–U6, simple negation handling (`no|not|without|never` within 3 tokens before), normalisation (lowercase, curly to straight quotes, `can't`/`cant`/`cannot`, common typos), and child keywords
  - `specialty_for(codes, text) -> str | None`
- [x] `src/netracare/guardrails/advice_filter.py`: `check_fact(text, topic) -> list[str]` (violation codes) and `check_summary(draft, notes) -> list[str]` (faithfulness flags: invented medicine, invented number or unit, condition word not in notes, empty sections).
- [x] `log.redact`: add `redact_names(text, names)` (word-boundary, case-insensitive).
- [x] Tests:
  - `tests/unit/guardrails/test_scripts_match_policy.py`: parses `docs/safety_policy.md` §5.1 and §5.2 and asserts the constants equal them, ignoring only markdown quote markers.
  - `test_redflag_rules.py`: every v0 emergency and urgent case; negations ("no flashes, just floaters" → U1, not E3); benign controls → no match; hidden emergency; typos.
  - `test_advice_filter.py`.

## T6. Triage
- [x] `src/netracare/services/triage_service.py`, `TriageService(session, clock, settings, llm: LLMClient | None)`:
  - `classify(text, *, actor=None, patient_id=None) -> TriageResult`, where `TriageResult` holds `level`, `codes`, `specialty_code`, `doctor_name`, `confidence`, `source` (`rules` | `llm+rules` | `rules_fallback` | `shortcircuit`), `message`, `alert_id?`, `needs_handoff`
  - Builds a LangChain chain: `ChatPromptTemplate` from the registry, then `RunnableLambda(llm.generate)`, then parse
  - `_combine(rules, llm_out)` takes the max severity and applies the low-confidence escalation
  - `_raise_alert(result, text, patient_id, actor)`: redacted 200-char excerpt plus audit `triage.alert`
  - `ack(actor, alert_id)` and `resolve(actor, alert_id, note)`: optimistic lock → 409 `already_handled`
  - `list_alerts(status)`
- [x] Short-circuit: text stripped of emoji and punctuation is empty → `non_clinical`; length > 2000 → 422.
- [x] `security/rate_limit.py`: `SlidingWindowLimiter(limit, window)` keyed by user ID or client IP; `POST /triage` returns 429 when exceeded.
- [x] `booking_service.book`: if `cmd.reason` and actor is a patient → `TriageService.classify`.
  - Emergency → `ConflictError("emergency_detected", details={message, alert_id}, commit=True)`.
  - Urgent → `BookingContext.red_flag_level=URGENT` (approval reason `post_red_flag`).
  - Staff → classify for the record only and store `red_flag_level`.
- [x] Router `api/routers/triage.py`:
  - `POST /triage` (public, optional auth): body `{text, patient_id?}` → `TriageOut`
  - `GET /triage/alerts?status=` (staff)
  - `POST /triage/alerts/{id}/ack` (staff)
  - `POST /triage/alerts/{id}/resolve` (staff, `{note}`)
- [x] Tests:
  - `tests/unit/services/test_triage.py`: combination truth table; LLM down → rules fallback; LLM says routine but rules say emergency → emergency; low confidence escalates; injection text still emergency; property test (hypothesis-free, all v0 cases) that final ≥ rules
  - `tests/api/test_triage.py`: verbatim script, E4 rinse line, alert created and audited, ack/resolve and double-ack 409, rate limit 429, empty and emoji input, length 422, anonymous allowed, alerts staff-only
  - `tests/api/test_appointments.py`: `test_emergency_reason_pauses_patient_booking`, `test_urgent_reason_requires_approval`, `test_staff_booking_not_blocked_records_level`

## T7. Eye-fact
- [x] `prompts/eye_fact/topics.yaml` (about 20 topics, each `{id, title, keywords}`) and `prompts/eye_fact/static_facts.yaml` (one reviewed fact per topic).
- [x] `src/netracare/services/eye_fact_service.py`, `EyeFactService(session, clock, settings, llm)`:
  - `today() -> EyeFactView`, where `EyeFactView` holds `day`, `topic`, `text`, `source`, `disclaimer`
  - `topic_for(day)`: a stable hash of the ISO date, mod the number of topics
  - Generation: generate, validate with `check_fact`, and on any violation or `LLMError` use the static fact
  - Insert with a conflict-safe read-back
- [x] `GET /eye-fact` (public). CLI `python -m netracare.llm.jobs eye-fact [--date YYYY-MM-DD]`.
- [x] Tests: deterministic topic; cached per day (provider called once across two requests); unsafe LLM output gives static (each advice pattern); LLM error gives static; budget exceeded gives static; concurrent first insert is safe; disclaimer text.

## T8. Visit summaries
- [x] `src/netracare/services/visit_summary_service.py`, `VisitSummaryService(session, clock, settings, llm)`:
  - `create_draft(actor, appointment_id, notes)`: doctor must own the appointment; status `checked_in` or `completed`; notes 20–8000 characters. Redacts the patient's name, phone and email; generates `VisitSummaryDraft`; runs `check_summary` for `needs_attention`; upserts the draft (`approved` → 409); audits `visit_summary.draft`.
  - `get_for(actor, appointment_id)`: doctor (own) gets draft or final; patient or guardian gets the approved one only, else 404; staff get status only.
  - `edit(actor, id, draft, acknowledged_flags)`: re-runs the checks.
  - `approve(actor, id)`: all flags must be acknowledged or cleared, else 409 `unresolved_flags`. Sets `final_json`, audits, and sends notification `visit_summary_ready`.
  - `discard(actor, id)`.
  - `LLMError` → 503 `llm_unavailable`, with the message "write the summary manually". Manual path: `edit` on an empty draft is allowed.
- [x] `NotificationService.TEMPLATES`: add `visit_summary_ready`.
- [x] Router additions in `api/routers/visit_summaries.py`:
  - `POST /appointments/{id}/visit-summary`
  - `GET /appointments/{id}/visit-summary`
  - `PATCH /appointments/{id}/visit-summary`
  - `POST /appointments/{id}/visit-summary/approve`
  - `POST /appointments/{id}/visit-summary/discard`
- [x] Tests:
  - Patient 404 before approval and 200 after
  - Another doctor gets 404
  - Front desk sees status only
  - Invented medicine and dose flagged; approve blocked until acknowledged
  - Edit clears flags
  - Notes redacted in the provider request (assert the patient name is not in `FakeProvider.requests`)
  - LLM down → 503, then manual draft and approve works
  - Approve twice → 409
  - Audit and notification rows exist

## T9. Admin usage and permission matrix
- [x] `GET /admin/llm-usage?days=30` (admin): totals by day, model and purpose; month to date versus budget; the last 50 calls without content.
- [x] Add every new route to `tests/api/test_permission_matrix.py::ROUTES`.

## T10. UI
- [x] `components.eye_fact_banner(client)`: `st.cache_data(ttl=3600)` keyed by date; shown in `app.py` above the page title, with the disclaimer.
- [x] Pages and navigation (`navigation.py`):
  - **Patient:** `patient_symptom_check.py`. Text area → `/triage`. Emergency gives `st.error` with the verbatim script and no booking button; urgent gives `st.warning`; routine shows the routing sentence and a "Book a visit" link. The disclaimer is always shown.
  - **Front desk:** `frontdesk_alerts.py` (open alerts first, emergency badge, ack and resolve), plus an open-emergency count banner on Today.
  - **Doctor:** `doctor_visit_notes.py`. Pick a checked-in or completed visit → notes → "Generate draft" → editable fields with flags in red and acknowledge checkboxes → Approve or Discard. The DRAFT disclaimer is shown.
  - **Admin:** `admin_llm_usage.py` (metrics and a daily cost chart via `st.bar_chart`, and a recent-calls table).
  - **Patient:** `patient_appointments.py` gets a "Visit summary" expander for approved summaries.
  - **Booking widget:** handles 409 `emergency_detected` by showing the script.
- [x] AppTest: every new page renders for its role; symptom check emergency shows the exact script; doctor draft-to-approve flow; the banner appears on the login page.

## T11. Evals and Opik
- [x] `evals/tasks.py`, new suites:
  - **`safety_redflags_rules`** on dataset `safety_redflags_v0`. Task: rules-only `TriageService`. Metrics: `ExactMatch(level)`, `LabelRecall(level, emergency)`, `UnderTriage` (new metric: 1 if predicted severity ≥ expected).
  - **`safety_redflags_llm`**: same dataset, full classifier, `requires_live=True`, skipped unless `--live`.
  - **`eye_fact_safety`**: new dataset of about 20 candidate facts labelled safe or unsafe. Task: `check_fact`. Metric: `ExactMatch(safe)`.
  - **`visit_summary_v0`**: new dataset of 8 notes with expected flags. Offline task uses `FakeProvider` canned drafts to test `check_summary`; live task uses Groq.
- [x] Runner:
  - `prompt_versions` metadata comes from the registry.
  - `--live` flag.
  - Live suites are excluded from `run all` unless `--live`.
- [x] Baselines: `safety_redflags_rules.json` (emergency recall 1.0, under-triage 1.0) and `eye_fact_safety.json`. CI `evals-offline` gates them.
- [x] `evals-opik` CI job: also runs live suites when the `GROQ_API_KEY` secret exists (non-blocking).
- [x] Tests for the new metric, live gating, and prompt-version metadata.

## T12. Groq smoke test
- [x] `scripts/groq_smoke.py`:
  - Lists models and asserts both configured models exist.
  - Runs a structured triage call on 3 fixed texts.
  - Generates one eye-fact.
  - Prints tokens, cost and latency.
  - Asserts an `llm_calls` row and (if Opik is on) flushes spans.
- [x] `tests/live/test_groq_live.py` (marker `live`, skipped unless `RUN_LIVE=1` and the key is set): structured-output round trip, both models.
- [x] `pyproject.toml`: `addopts` adds `-m "not live"`.

## T13. Close-out
- [x] Export requirements and do a clean venv install.
- [x] `ruff check`, `ruff format --check`, `mypy`, `pytest --cov` (≥ 90% overall; ≥ 95% for `services/` and `llm/`).
- [x] Live: smoke test, `evals run safety_redflags_llm --live --upload`, and the demo script.
- [x] `docs/phase-2/RETRO.md`; tick this file and the global `docs/task.md`; add deviations to the Decisions Log.
- [x] Commit `phase-2: first LLM features - triage, eye-fact, visit summaries, LLM client`; push; CI green.
