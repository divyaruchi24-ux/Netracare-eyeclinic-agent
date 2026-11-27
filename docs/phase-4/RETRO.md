# Phase 4 — Retrospective (booking agent, LangGraph)

**Dates:** 2026-11-26 (planning) – 2026-11-27 (execution) · **Commit:** `phase-4: LangGraph booking agent with HITL approvals and checkpointing`

## What shipped
- **LangGraph booking agent** (`netracare/agents/booking/`) inside the patient chat: book, reschedule, cancel and list appointments in plain language.
  - **One graph run = one patient turn:** `ingest → route → collect | choose | confirm | list | abort | decided | resume`.
  - The per-request runtime (DB session, caller, LLM) goes in through LangGraph's `context`, so it is never checkpointed.
- **The model proposes, code disposes.**
  - `booking_nlu@v1` (fast model) only *extracts* phrases.
  - Code does the rest: dates (`domain/relative_dates.py`), doctors (fuzzy names, specialist routing), option choices (ordinals, times, buttons), and whether a message is an **explicit yes** (`confirm.py`).
  - Every reply is a fixed template filled from tool results.
- **Tools** (`tools.py`): `search_doctors`, `get_availability`, `get_my_appointments`, `book_appointment`, `reschedule_appointment`, `cancel_appointment`, `escalate_to_human`.
  - They act as the caller (access policy), take typed pydantic arguments and never raise into the graph.
  - Writes run inside a **savepoint**: no partial writes.
  - They are **idempotent** (`agent:{thread}:{proposal}` keys), so a repeated "yes" returns the original appointment.
  - They are also exposed as LangChain `StructuredTool`s for Phase 6.
- **Read-back + explicit yes before every write.** The read-back shows the full date, the doctor, the patient and any approval reason (from `BookingService.preview_approval`, which writes nothing). Hedges ("yes but…", "yes?", "maybe") never write.
- **HITL:**
  - Policy exceptions become pending requests tagged `source=chat`.
  - The front desk can approve, **edit** (`approve_with_changes`: a slot swap in one transaction, with a 409 on a clash) or reject.
  - A per-app decision hook resumes the agent's thread and posts the outcome into the conversation; notifications go out as usual.
  - Requests pending more than 4 h get the §12 text once (`agents.booking.jobs overdue` + an on-message check).
- **Durable conversations.** `SqliteSaver` in its own file (`data/agent_checkpoints.db`). Idle flows expire after 24 h; proposals are re-checked after 30 min; the 90-day chat purge also deletes checkpoints.
- **Safety inside the flow:**
  - An emergency mid-booking pauses the flow (policy 5.1) until "continue booking".
  - An emergency booking reason pauses it with the script.
  - Urgent + "book me" gives the urgent script, then options; the booking needs `post_red_flag` approval.
  - Questions mid-flow are answered by RAG with "We can continue your booking whenever you're ready."
- **UI:**
  - Chat: option buttons, a read-back with **Yes, book it / No / Change**, and an "Awaiting front desk" badge.
  - Approvals: a "via chat" badge and an **Edit** picker ("Approve with this time").
- **Evals:**
  - `relative_dates` (58 cases) and `booking_agent_offline` (33 multi-turn conversations) are new **CI gates**.
  - `booking_agent_live` runs on Groq and logs to Opik.
- **Migration 0005:**
  - `approval_requests.source`, `chat_session_id` and `notified_overdue_at`
  - `chat_messages.agent`

## Verification
| Check | Result |
|---|---|
| Tests / coverage | **1,258 tests**, all passing except the opt-in live modules (skipped by default); 97% overall; `agents/` 97%, `services/` 97%, `llm/` 99%, `rag/` 98%. ruff, ruff format and mypy strict are clean (176 files) |
| Clean venv `pip install -r requirements.txt -r requirements-dev.txt` | OK, `pip check` clean; langgraph 1.2.24, langgraph-checkpoint-sqlite 3.1.3 import (the first attempt hit a transient PyPI error for `requests-oauthlib`; the retry passed) |
| `booking_agent_offline` (CI gate) | **no_write_without_yes 1.0**, task_success 1.0, tool_call_correctness 1.0 (33 conversations) |
| `booking_agent_live` (Groq, Opik) | **no_write_without_yes 1.0**, task_success 1.0, tool_call_correctness 1.0 (second run; the first was 0.97, see below) |
| `relative_dates` (CI gate) | exact_range 1.0 (58 phrases); DST conversions unit-tested in Europe/London |
| Restart | unit test (new `BookingAgent` over the same SQLite file resumes at the read-back and books) **and live**: the dev API was stopped and restarted between the read-back and "yes", and the booking completed |
| Groq smoke test | PASS, including the new booking step (LLM understanding → code-resolved date → 3 options, no write) |
| Live demo (port 8001, real Groq) | floaters + "book next Tuesday morning" → urgent script + 3 Retina options → read-back noting approval → pending → the front desk **edited** to 10:15 and approved → the chat showed "confirmed at a different time" and the outbox got `approval_changed`; "the day after tomorrow" (a Sunday) → closed-day explanation with alternatives; a hidden bleach emergency → verbatim script; guardian "book for my son" → Dr. Arjun Menon with the child-approval note; restart survival as above. Groq spend for the whole demo was about $0.0001 |

## What went well
- Keeping the LLM to extraction made the hardest property, "no write without a yes", a code property: 1.0 offline and live, with a test that checks every write's turn history.
- The offline conversation harness (fresh DB copy, frozen clock, scripted events for stolen slots, failures, front-desk decisions and pauses) made multi-turn corner cases cheap to test and deterministic in CI.
- Reusing the Phase 1 approval queue meant HITL needed one new service method (edit) and a hook, not a second workflow.
- The live eval found one real gap quickly ("what are my appointments" labelled `none` by the model); it is now detected by code too.

## What was hard / surprises
- **Interrupts vs HTTP.** LangGraph `interrupt()` across requests would block new flows in a chat while a request waits for staff. Modelling the wait as checkpointed state, resumed by an event, kept the same durability without the lock-in (Decisions Log).
- **SQLite locking.** The request holds `BEGIN IMMEDIATE` while the agent makes LLM calls whose usage rows are written from another session. The fixes:
  - release the request transaction around the agent;
  - take a fresh write transaction right before a tool write;
  - let triage run without `outside_transaction` when it is already inside a savepoint.
  Tests that reuse one session now commit per turn, as real requests do.
- **Policy subtleties:**
  - the 24 h rule for reschedules is judged by the *current* appointment's start;
  - Dr. Nair has no Thursday clinic (a test expectation was wrong, not the code);
  - an expired flow intentionally forgets the doctor.
- **The public `.env.example` was found hand-edited in the Phase 3 commit:** a dozen settings were missing and the Opik workspace name was filled in. No key leaked. It was repaired, and a test now fails if a setting goes missing or a secret line gets a value.
- Shell heredocs with apostrophes broke again; multi-line edits now go through script files written with the editor.

## Carry-overs
- Staff booking through chat (staff keep the booking pages).
- Multi-appointment selection by date range ("my December appointment") is handled by doctor or single-day match only.
- The booking reason triage inside the tool runs within the transaction (one short LLM call); move it before the savepoint when Postgres arrives (Phase 7).
- Phase 3 carry-overs (router borderlines, threshold recalibration) and Phase 1/2 carry-overs remain for Phases 5 and 7.

## Metrics snapshot
- Prompts: `booking_nlu@v1` (new), plus the Phase 2/3 prompts.
- Groq spend during Phase 4 execution (two live booking eval runs, smoke tests, demo): well under $0.05.
