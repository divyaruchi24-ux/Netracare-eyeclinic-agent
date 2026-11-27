# Phase 4: Tasks (exact changes)

Paths are relative to the repo root. Verify each group with `.\scripts\dev.ps1 check`.
- Unit, API and offline-eval tests use `FakeProvider` (scripted NLU), `InMemorySaver`, and the seeded test DB with a frozen `MutableClock`.
- Live tests are marked `live`.

## T0. Planning
- [x] `docs/phase-4/understanding.md`, `plan.md`, `task.md`
- [x] User approval: "Approve Phase 4 plan?" (2026-11-27)

## T1. Foundations
- [x] `uv add langgraph langgraph-checkpoint-sqlite` (lock: langgraph 1.2.24, langgraph-checkpoint-sqlite 3.1.3). Check that langchain-core stays at 1.8.x.
- [x] `config.py` + `.env.example`:
  - `agent_checkpoint_path: str = "./data/agent_checkpoints.db"`
  - `agent_options_count: int = 3`
  - `agent_proposal_ttl_minutes: int = 30`
  - `agent_flow_expiry_hours: int = 24`
  - `agent_search_days: int = 14`
  - `agent_max_tool_failures: int = 2`
  - `approval_overdue_hours: int = 4` (replaces the `OVERDUE_AFTER` constant)
- [x] `domain/enums.py`:
  - `BookingAction(book, reschedule, cancel, list, none)`
  - `AgentStep(collect, propose, readback, awaiting_staff, done, aborted, paused_safety, expired)`
  - `AgentTurnKind(question, options, readback, confirmed, pending, decided, info, error)`
- [x] `pyproject.toml`: mypy overrides for `langgraph.*` if needed.

## T2. Relative dates
- [x] `src/netracare/domain/relative_dates.py`:
  - `DateRange(start: date, end: date, label: str)`
  - `TimeWindow(start: time, end: time, label: str)`
  - `resolve_dates(phrase: str, today: date) -> DateRange | None`
  - `resolve_time(phrase: str) -> TimeWindow | None`
  - `explain_unavailable(day: date, holidays: set[date]) -> str | None`: Sunday, holiday or past
  - Rules: today, tomorrow, day after tomorrow, weekday / this weekday / next weekday, this weekend (Saturday), next week, in N days, `12 Dec`, `12th`, `12/12` (day-first), morning (09–13), afternoon (14–18), evening (16–18), after/before HH, around HH
- [x] `evals/datasets/relative_dates_v0.jsonl` (≥ 40 cases: anchors on a Monday, Saturday and Sunday, month-end, year-end, and Europe/London DST weeks); suite `relative_dates` (offline gate, `exact_range`).
- [x] Tests `tests/unit/domain/test_relative_dates.py`: dataset-driven cases plus DST conversions of windows (`zoneinfo` round-trip).

## T3. Tools
- [x] `src/netracare/agents/booking/tools.py`:
  - `ToolContext(session, clock, settings, actor, thread_id)`
  - `ToolResult(ok: bool, data: dict | None, error: str | None, message: str)`
  - Pydantic args per tool: `SearchDoctorsArgs`, `AvailabilityArgs`, `BookArgs`, `CancelArgs`, `RescheduleArgs`, `MyAppointmentsArgs`, `EscalateArgs`
  - Functions: `search_doctors`, `get_availability`, `book_appointment`, `cancel_appointment`, `reschedule_appointment`, `get_my_appointments`, `escalate_to_human`. They map `AppError` codes to `ToolResult(error=...)` and never raise. Idempotency keys are `agent:{thread_id}:{proposal_id}`.
  - `as_langchain_tools(ctx) -> list[StructuredTool]` (the same functions, for Phase 6)
- [x] Tests `tests/unit/agents/booking/test_tools.py`:
  - each tool's happy path
  - another patient's appointment → `not_found`
  - a slot taken → `slot_unavailable`
  - an idempotent replay returns the same appointment
  - cancel twice → `already_cancelled`
  - a fault-injected DB error → `ToolResult(error="internal")` with the DB unchanged

## T4. NLU and confirmation
- [x] `llm/schemas.py`: `BookingTurn(action, doctor_mention?, specialty_hint?, concern_phrase?, date_phrase?, time_phrase?, patient_mention?, appointment_ref?, option_choice?: int, reply_kind: answer|yes|no|change|stop|other, confidence)`; register it in `SCHEMAS`.
- [x] `prompts/booking_nlu/v1.yaml` (fast tier): extraction only, never invent dates; copy phrases as written; data-only delimiters; examples for changes ("make it 11"), ordinals ("the second one") and stops.
- [x] `agents/booking/nlu.py`:
  - `understand(llm, text, state_summary) -> BookingTurn`
  - `keyword_fallback(text) -> BookingTurn`
  - `choose_option(text, turn, options) -> int | None`: ordinals, times, weekdays, "first/last"
- [x] `agents/booking/confirm.py`: `is_explicit_yes(text) -> bool`, `is_stop(text) -> bool` (with Hinglish forms).
- [x] Tests: `test_nlu.py` (FakeProvider; fallback when the LLM is down), `test_confirm.py` (yes / hedge / negation table, ≥ 40 rows).

## T5. Graph
- [x] `agents/booking/state.py`: `BookingState` (TypedDict):
  - `action`, `patient_id`, `doctor_id`, `specialty`, `date_range`, `time_window`, `appt_type`
  - `appointment_id`, `options: list[OptionDict]`, `proposal_id`, `proposed_at`, `chosen`
  - `step: AgentStep`, `failures`, `approval_request_id`, `last_question`, `updated_at`
- [x] `agents/booking/templates.py`: fixed texts (questions, options intro, read-back, confirmations, pending, decided: approved/changed/rejected, overdue (§12), sign-in, nothing available, closed day, past date, aborted, expired, paused for safety).
- [x] `agents/booking/graph.py`:
  - `build_graph(tools, nlu, settings, clock) -> CompiledStateGraph`
  - nodes: `understand`, `resolve_patient`, `resolve_target`, `resolve_dates`, `availability`, `propose`, `await_choice`, `readback`, `await_yes`, `execute`, `outcome`, `list_appointments`, `pick_appointment`, `escalate`
  - `interrupt()` at the await nodes and at `awaiting_staff`
- [x] `agents/booking/agent.py`:
  - `BookingAgent(checkpointer, settings, clock)`
  - `handle(session, actor, thread_id, text, *, llm) -> AgentTurn(kind, text, options, readback, state_step, write: WriteRecord | None, llm_call_ids)`
  - `is_active(thread_id) -> bool`
  - `on_approval_decided(thread_id, decision)`
  - `pause_for_safety(thread_id)`
  - `expire_if_stale(thread_id)`
  - `purge(thread_ids)`
- [x] Tests `tests/unit/agents/booking/test_graph.py`:
  - book, reschedule, cancel and list happy paths
  - a missing-slot question per slot
  - a mind change at each await node; "stop" aborts
  - a hedged yes doesn't write
  - slot taken → re-propose
  - 2 tool failures → escalate
  - expiry after 24 h
  - safety pause/resume
  - a guardian picks a child
  - a property test: every `write` follows readback + explicit yes in the same thread

## T6. Checkpointing
- [x] `agents/booking/checkpoint.py`: `make_checkpointer(settings) -> BaseCheckpointSaver` (`SqliteSaver` on its own connection, WAL); `InMemorySaver` for tests.
- [x] `services/chat_retention.py`: also delete checkpoints of purged sessions.
- [x] Tests: `test_checkpoint.py` (a restart with a new agent over the same file resumes at `await_yes`, then books); a purge test.

## T7. HITL
- [x] `services/approval_service.py`:
  - `approve_with_changes(actor, request_id, new_slot_ids, note) -> ApprovalView` (NEW_BOOKING/RESCHEDULE: swap the held slots in one transaction; audit `approval.approve_with_changes`; notification `approval_changed`)
  - an `on_decided` hook list (registered by the app) called after approve/reject/edit
  - `approval_overdue_hours` from settings
- [x] `services/notification_service.py`: templates `approval_changed`, `approval_overdue`.
- [x] `agents/booking/jobs.py`: CLI `python -m netracare.agents.booking.jobs overdue` (one §12 notice per overdue request; idempotent via a `notified_overdue_at` column on `approval_requests`, migration 0005), plus an on-open check in `BookingAgent`.
- [x] Migration 0005: `approval_requests.source` (ui|chat), `approval_requests.chat_session_id?`, `approval_requests.notified_overdue_at?`.
- [x] Tests: approve-with-changes (slot swap, conflict 409 when the new slot was taken, audit + notification); hooks resume the thread; overdue job runs once.

## T8. Chat integration
- [x] `services/chat_service.py`:
  - when `BookingAgent.is_active(session)` and layer 1 doesn't decide, the message goes to the agent (no router call)
  - router `booking` intent → agent (signed-in patients) or the sign-in prompt (anonymous)
  - layer-1 emergency/urgent while active → `pause_for_safety`
  - FAQ while active → answer and add "We can continue your booking whenever you're ready."
- [x] `ChatTurn` gains `options`, `readback`, `agent_step`; persisted in `chat_messages.safety`/a new `agent` JSON field (migration 0005).
- [x] Tests `tests/unit/services/test_chat_agent.py`: end to end through `ChatService` with FakeProvider.

## T9. API
- [x] `api/schemas/chat.py`: `AgentOptionOut(id, label, start_local, doctor_name, specialty)`, `ReadbackOut(text, needs_approval, reasons)`; `ChatTurnOut` gets `options`, `readback`, `agent_step`.
- [x] `api/routers/approvals.py`: `POST /approvals/{id}/approve-with-changes {slot_ids, note}`; list items include `source`, `chat_session_id`.
- [x] `api/main.py`: build `BookingAgent` + checkpointer at startup (tests inject `InMemorySaver`); register `ApprovalService` hooks.
- [x] Tests: `tests/api/test_chat_booking.py` (book → confirmed; < 24 h → pending → approve-with-changes → the chat shows the decision; anonymous → sign-in; another patient's appointment → not found); permission matrix rows.

## T10. UI
- [x] `ui/app_pages/chat.py`: option buttons (send `option:<id>`), a read-back card with **Yes, book it** / **No** / **Change** buttons, an "Awaiting front desk" chip.
- [x] `ui/app_pages/frontdesk_approvals.py`: a "via chat" badge; an **Edit** expander with an availability picker → "Approve with this time".
- [x] `ui/api_client.py`: `approve_with_changes`.
- [x] Tests `tests/ui/test_booking_agent_pages.py`: book through buttons; the front desk edits and approves; the chat shows the decision.

## T11. Evals and smoke test
- [x] `evals/datasets/booking_agent_v0.jsonl` (≥ 30 conversations): `turns[{user, nlu}]`, `expected{final_step, writes[{tool, ...}], tools[...]}`, tags.
- [x] `evals/booking_tasks.py`:
  - `booking_agent_offline` (scripted NLU per turn over a fresh copy of the seeded DB and a frozen clock)
  - `booking_agent_live` (real NLU; the conversation's `nlu` is ignored)
  - metrics `NoWriteWithoutYes`, `TaskSuccess`, `ToolCallCorrectness`, `TurnsToCompletion`
- [x] Baselines; add to `dev.ps1 evals`, the CI offline gate, `evals-opik` (live), and `dev.ps1 evals-live`.
- [x] `scripts/groq_smoke.py`: a booking turn ("book a check-up with Dr. Nair next Tuesday morning" → options) against a temp copy of the DB, no write.
- [x] `tests/live/test_booking_live.py`.
- [x] `docs/evals.md`: the new suites.

## T12. Close-out
- [x] `uv export` → requirements; clean-venv install + `pip check` + import smoke (langgraph, langgraph.checkpoint.sqlite).
- [x] `.\scripts\dev.ps1 check`; offline evals; live evals with `--upload`; Groq smoke test.
- [x] Live demo per [plan.md](plan.md) against port 8001 (migrate the dev DB to 0005 first).
- [x] `docs/phase-4/RETRO.md`; tick this file and `plan.md` DoD; update `docs/task.md`, `docs/understanding.md`, `docs/plan.md` if needed, and README.
- [x] Commit `phase-4: LangGraph booking agent with HITL approvals and checkpointing` (identity `divyaruchi24-ux`), push, CI green.
