# Phase 4: Understanding (booking agent, LangGraph)

> Scratchpad. Messy is fine. The dated Decisions Log at the bottom is append-only.

## 1. What the brief asks for (restated)

1. A **LangGraph state machine**:
   - understand intent
   - collect slots (doctor/specialty, date, time, patient identity)
   - check availability (tool)
   - propose options
   - confirm with the user
   - **HITL interrupt for staff approval where policy requires**
   - execute the tool
   - confirm and notify
2. **Tools:** `search_doctors`, `get_availability`, `book_appointment`, `cancel_appointment`, `reschedule_appointment`, `get_my_appointments`, `escalate_to_human`. They all call the service layer (the single source of truth), validate their inputs and are idempotent.
3. **Checkpointing** (SQLite saver), so a conversation survives a restart. The front-desk UI shows pending approvals with **approve / edit / reject**.
4. The agent **always reads back the details and gets an explicit "yes" before any write**.

Global exit criteria ([plan.md](../plan.md)):
- no write without an explicit "yes";
- HITL approve/edit/reject from the front-desk UI;
- a conversation survives a restart;
- a tool failure leaves no partial writes.

Corner cases this phase owns ([task.md tracker](../task.md)):
- emergency hidden in booking chatter (agent level)
- doctor on leave, or a slot removed or taken after it was proposed
- holidays, past dates, out-of-hours requests
- relative dates ("next Tuesday", "day after tomorrow", "this weekend") and time zone / DST
- policy-window reschedules and cancellations, no-show-flagged patients, another patient's appointment (authorisation)
- the patient changes their mind mid-flow, abandons the conversation, or resumes later
- a tool fails mid-transaction
- a staff approval times out (what the patient sees)
- third-party and guardian bookings (minors → pediatrics + approval)

## 2. What already exists (we build on it, we don't duplicate it)
- **BookingService** (Phase 1):
  - `book(actor, BookCommand)`, `cancel(actor, id, reason)`, `reschedule(actor, id, new_slot_ids, reason, idempotency_key)`
  - concurrency-safe: partial unique index + `BEGIN IMMEDIATE`; idempotency keys; full audit
  - the approval policy (`domain.policies.requires_approval`) creates `pending_approval` appointments + `ApprovalRequest`s
  - Phase 2/3 triage of the booking reason
- **AvailabilityService.find(doctor_id|specialty_code, from_date, to_date, appt_type, now)** → `AvailableOption(doctor_id, start_utc, end_utc, slot_ids)`. It already excludes leaves, holidays, lunch, Sundays, the past and held slots, and caps the range at `MAX_RANGE_DAYS`.
- **ApprovalService** + the front-desk **Approvals** page:
  - approve/reject with notes, overdue after 4 h (`OVERDUE_AFTER`)
  - notifications: `appointment_pending_approval`, `approval_approved`, `approval_rejected`
- **Phase 3 chat:**
  - `ChatService` with a layer-1 safety check on every message, the LLM router (the `booking` intent today answers with a "Book with Dr. X" page link), `SpecialistRouter` (concern → specialty → doctor), and handoffs
  - sessions are persisted and owned (patients or anonymous)
- `Clock` (injectable, tz-aware) and `MutableClock` in tests; IST has no DST, but the `Clock`/tz abstraction must be proven against a DST zone.
- `/auth/me.patient_ids` lists the patients a user may book for (self + linked children).

## 3. Design options and choices

### 3.1 Where the agent lives
Options:
- (a) a separate `/agent` endpoint and UI page;
- (b) **inside the existing chat**: `ChatService` hands `booking` intents (and follow-up turns of an active booking) to the agent.

**Chosen: (b).** One conversation, one safety layer and one history, which is what a patient expects ("how much is LASIK?" then "ok book me in"). `ChatService` keeps layer-1 safety **before** the agent on every turn, so an emergency always overrides an in-progress booking (policy 4.3/5.1: pause the flow and show the script).

### 3.2 LLM vs code responsibilities ("the model proposes, code disposes")
| Step | Who |
|---|---|
| Understand the turn: action, doctor/specialty mention, **date phrase as written**, time preference, which patient, which appointment, option choice, yes/no/change | **LLM** (`booking_nlu@v1`, fast model, strict JSON), with a keyword fallback when the LLM is down |
| Resolve dates ("next Tuesday" → a date) | **code** (`domain/relative_dates.py`, anchored to the clinic-local `Clock`) |
| Which doctor/specialty | `DirectoryService` + the Phase 3 fuzzy name matcher + `SpecialistRouter` |
| Availability, booking, cancel, reschedule | **services**, via tools |
| Read-back text and every confirmation | **fixed templates** filled from tool results (never generated text) |
| "Is this an explicit yes?" | **code** (`agents/booking/confirm.py`): an affirmative pattern and no negation/hedge; the LLM's opinion is ignored for writes |

This keeps the agent safe and testable: the LLM never invents a time, a doctor or a price, and it can't trigger a write.

### 3.3 Graph (LangGraph `StateGraph`)
```
START -> guard? (done by ChatService) -> understand -> route_action
  book:       resolve_patient -> resolve_target(doctor|specialty) -> resolve_dates
              -> availability -> propose (<= 3 options) -> await_choice
              -> readback -> await_yes -> execute_book -> outcome
  reschedule: pick_appointment -> resolve_dates -> availability -> propose -> await_choice
              -> readback -> await_yes -> execute_reschedule -> outcome
  cancel:     pick_appointment -> readback -> await_yes -> execute_cancel -> outcome
  list:       list_appointments -> END
  outcome: confirmed -> notify text -> END
           pending_approval -> interrupt("awaiting_staff") -> (front desk decides) -> resume -> END
           conflict (slot taken / doctor on leave) -> availability again (no write happened)
           failure -> retry once if transient -> escalate_to_human (handoff) on a 2nd failure
```
- **"Ask the user" nodes** (`await_choice`, `await_yes`, missing-slot questions) end the turn with a question. The next user message re-enters the graph at the right node from the checkpointed state. This uses LangGraph `interrupt()` / `Command(resume=…)`, so each turn is a resume.
- **Mind changes**: at any `await_*` node, "actually Thursday", "a different doctor" or "make it 5pm" re-run from `resolve_*` with the updated slots. "Never mind", "stop" or "cancel that" abort, with no write. A new unrelated question (FAQ) is answered by `ChatService` and the booking state stays parked (resumable with "back to my booking" or by a choice/yes).
- **Re-validation**: options proposed earlier are re-checked at `execute_*` (the services enforce this anyway). A slot taken in between → `slot_unavailable` → fresh options. A proposal older than 30 minutes is re-checked before the read-back.

### 3.4 HITL staff approval
- The policy decides (`requires_approval`):
  - < 24 h changes
  - new-patient surgery consults
  - guardian bookings for minors
  - post-red-flag bookings
  - no-show-flagged patients
- The service creates a `pending_approval` appointment + `ApprovalRequest`, as today. The agent then **interrupts** with `awaiting_staff` and tells the patient the fixed text ("Your request is with our front desk… you'll get a message").
- The front desk acts in the existing **Approvals** page, which gains **Edit**: approve with a different slot (and/or doctor) from availability. `ApprovalService.approve_with_changes(actor, request_id, new_slot_ids, note)` reuses the reschedule machinery in one transaction: the original pending hold is released and the new slots are held and confirmed.
- On a decision, `ApprovalService` calls an **agent resume hook** (`BookingAgent.on_approval_decided(request_id)`). It resumes the checkpointed thread with the decision, so the next time the patient opens the chat they see "Approved: … / Not approved: … / Changed to …", and the notification outbox gets the usual message.
- **Approval timeout** (policy §12): at `OVERDUE_AFTER` (4 h), a periodic job (`python -m netracare.agents.booking.jobs overdue`, plus a check whenever the patient opens the chat) sends the patient the §12 text once: "Our team hasn't confirmed this yet; we'll contact you, or you can call +91 80 0000 1234". The appointment stays pending.

### 3.5 Checkpointing
- `langgraph-checkpoint-sqlite` `SqliteSaver` in a **separate file** (`data/agent_checkpoints.db`), so checkpoint writes never contend for the main DB's `BEGIN IMMEDIATE` lock. `thread_id = chat_session_id`.
- Tests use `InMemorySaver`, plus one test with `SqliteSaver` in `tmp_path` that rebuilds the agent object (simulating a restart) and resumes the conversation.
- The state is small and PII-light: ids, dates, option slot ids and the flow step; no free text beyond the last question. The retention purge (Phase 3, 90 days) also deletes checkpoints of purged sessions.
- Abandoned flows: a booking state untouched for 24 h expires. The next message starts fresh, mentioning "your earlier booking request was not completed".

### 3.6 Tools (`agents/booking/tools.py`)
Each tool is a typed function with a pydantic args model that calls the service with the **caller's `CurrentUser`** (least privilege: a patient tool can only reach that patient's appointments, enforced by `AccessPolicy`). Each returns a typed `ToolResult(ok, data | error_code, message)` and never raises into the graph. They are wrapped as LangChain `StructuredTool`s, so they can also be listed for Phase 6 MCP/Skills.

| Tool | Service | Idempotency |
|---|---|---|
| `search_doctors(specialty?, name?)` | DirectoryService + fuzzy name | read-only |
| `get_availability(doctor_id?, specialty?, from, to, type)` | AvailabilityService.find | read-only |
| `book_appointment(patient_id, doctor_id, slot_ids, type, reason)` | BookingService.book | idempotency key = `agent:{thread}:{proposal_id}`, so a retried "yes" replays and never double-books |
| `cancel_appointment(appointment_id, reason)` | BookingService.cancel | cancelling an already-cancelled appointment → `already_cancelled`, no second write |
| `reschedule_appointment(appointment_id, slot_ids, reason)` | BookingService.reschedule | key = `agent:{thread}:{proposal_id}` |
| `get_my_appointments(patient_id?, upcoming=True)` | appointment repo via AccessPolicy | read-only |
| `escalate_to_human(reason)` | Phase 3 HandoffService | idempotent per open handoff |

Every write tool runs inside the request's DB transaction. A failure rolls back everything (no partial writes, already guaranteed by the services and verified by a fault-injection test). The `idempotency_records` table makes a retry after a crash return the original result.

### 3.7 Relative dates (`domain/relative_dates.py`)
- `resolve(phrase, now_local, tz) -> DateRange | None`. It handles:
  - today, tomorrow, day after tomorrow
  - weekday names: "this Friday" and "Friday" mean the next occurrence including today if still open, "next Tuesday" means the one in the following week; we document the rule and confirm it in the read-back
  - "this weekend" (Saturday only, as Sundays are closed), "next week" (Mon–Sat), "in 3 days"
  - "on 12th", "12 Dec", "12/12" (day-first, India)
  - "morning" / "afternoon" / "evening" time windows, and "after 4pm", "around 10"
- Ambiguity is resolved by the read-back: the read-back always shows the full date ("Tuesday 1 December 2026, 10:15").
- Past dates → "that date has passed". Sundays and holidays → named, with the next working days offered. Times outside 09:00–18:00 → "the clinic is open 09:00–18:00".
- DST: everything is computed in clinic-local dates and converted with `zoneinfo`. Tests run the resolver and availability windows in `Europe/London` across both 2026 DST changes.

### 3.8 Patient identity and guardians
- Signed-in patients only; anonymous chat gets "Please sign in to book. I can still answer questions."
- If `patient_ids` has more than one entry (a guardian), the agent asks which patient, by name. A minor → PEDIATRIC by default (Phase 3 rule) and the approval reason `guardian_booking_minor` (policy 7). The agent never books for someone who isn't linked; a request for an unlinked person gets the policy 7 text.
- Staff booking through chat is out of scope (staff have the booking pages).

### 3.9 Read-back and explicit yes
- **Read-back template** (fixed): "Please confirm: **{type}** with **{doctor}** ({specialty}) on **{weekday d Month yyyy} at {HH:MM}** for **{patient}**{approval_note}. Shall I book it? (yes / no)". `approval_note` comes from the policy ("This needs front-desk approval because …").
- **Explicit yes** (`is_explicit_yes`): matches yes, yeah, yep, confirm, book it, go ahead, please do, sure, ok book, haan/ha (Hinglish) as the whole intent. It is rejected when the message contains a negation or hedge ("yes but…", "not sure", "maybe", "wait", "no"), a question mark, or a new constraint. Anything else → no write, and the read-back is repeated or the change applied.
- The button in the UI sends the literal "yes".
- A property test over the conversation dataset asserts that every write is preceded, in the same thread, by a read-back and an explicit-yes turn.

### 3.10 Safety inside the flow
- `ChatService` layer 1 runs first on every message.
- **Emergency/urgent mid-flow → the flow pauses** (state `paused_safety`). The script is shown, and the booking can continue only after the patient says they are getting help ("I'm okay / I've called / continue booking"), per policy 5.1.
- The booking **reason** goes through the Phase 2 triage inside `BookingService` (unchanged): an emergency reason → 409 `emergency_detected` → the agent shows the script and pauses; urgent → `post_red_flag` approval.
- Medical questions mid-flow → the §5.3 refusal, then "shall we continue with your booking?".

### 3.11 Evals
| Suite | Live? | Gate | Content |
|---|---|---|---|
| `booking_agent_offline` | offline (scripted NLU) | **CI gate** | ≥ 30 multi-turn conversations over a seeded DB with a frozen clock; NLU outputs are scripted per turn, so this tests the graph, tools and policy. Metrics: `no_write_without_yes` (**must be 1.0**), `task_success`, `tool_call_correctness` (expected tool sequence), `final_state` |
| `relative_dates` | offline | **CI gate** | ≥ 40 phrases × anchor dates (incl. a DST zone) → expected ranges |
| `booking_agent_live` | live | report | the same ≥ 30 conversations with the real `booking_nlu@v1` on Groq; task success, write-safety (must be 1.0), turns-to-completion; Opik experiment |

Conversations cover:
- happy paths (book, reschedule, cancel, list)
- relative dates
- the slot taken after the proposal; the doctor on leave; a holiday; a past date; out of hours
- < 24 h changes → approval; a guardian booking for a child; a no-show-flagged patient
- mind changes; abandon and resume (checkpoint); hedged "yes but"
- an emergency mid-flow; a medical question mid-flow
- another patient's appointment id; a tool failure → escalation

### 3.12 UI
- **Chat page:**
  - options as buttons ("Tue 1 Dec, 10:15 · Dr. Vikram Shetty")
  - a read-back card with **Yes, book it** / **No** / **Change** buttons
  - an "Awaiting front desk" status chip while pending, then the decision
- **Front-desk Approvals page:** **Edit** opens an availability picker for the same doctor or specialty, then "Approve with this time"; agent-originated requests are labelled "via chat".
- **My appointments:** unchanged; agent bookings show up as normal.

## 4. Corner cases → handling → test
| Case | Handling | Test |
|---|---|---|
| "book next Tuesday morning with the retina doctor" | NLU → resolver → availability → 3 options | eval + graph unit test |
| "yes but make it 11" | not an explicit yes → change applied, re-proposed | `is_explicit_yes` tests + eval |
| Slot taken between proposal and yes | `slot_unavailable` → fresh options, no write | concurrency test with two threads |
| Doctor goes on leave after the proposal | the service refuses → options from other doctors of the specialty | eval |
| Sunday / holiday / past date / 8pm | named reason + next working day | resolver + eval |
| Reschedule tomorrow's visit (< 24 h) | `within_24h_change` → pending → interrupt → front desk approves → resume | API + UI test |
| Guardian books for their child | asks which patient → pediatric → `guardian_booking_minor` | eval |
| "cancel appointment X" for someone else | AccessPolicy 404 → "I can't find that appointment" | permission test |
| Restart between "options" and "yes" | `SqliteSaver` resumes in a new agent instance | restart test |
| Abandon for 2 days | state expires; fresh start with a note | unit test |
| Tool raises / DB error | rollback; retry once if transient; 2nd failure → handoff | fault-injection test |
| Approval overdue | §12 text once, via job / on open | job test |
| "lol bleach in my eye, anyway book Tuesday" | layer 1 → emergency script, flow paused | eval |
| LLM down | keyword NLU ("book", dates, "yes"); fixed questions | unit test with `DisabledProvider` |

## 5. Risks
| Risk | Mitigation |
|---|---|
| LangGraph API churn | Pin 1.2.24; wrap it in `agents/booking/graph.py` with our own types; graph tests through our interface |
| Checkpoint DB growth | Small state; purge with chat retention; separate file |
| LLM misreads choices ("the second one") | Code resolves ordinal references against the proposed list; buttons send exact option ids |
| Front-desk edit races with a patient cancel | Optimistic locking (`version`) on ApprovalRequest/Appointment already exists → 409 |
| Interrupt/resume across HTTP requests on SQLite | One agent invocation per request; the checkpointer uses its own connection; tests cover concurrent sessions |

## 6. Open questions (defaults assumed, non-blocking)
- **Can patients book through chat for an adult family member they're linked to?** *Assume yes, only for linked patients (`patient_ids`), as on the booking page.*
- **How many options to propose?** *Assume 3, spread over the requested range (earliest, plus two others at different times of day), with "show more".*
- **Should a pending (awaiting-approval) booking hold the slot?** *Yes, as today (the Phase 1 behaviour), so the patient doesn't lose it while staff review.*

## Decisions Log
- **2026-11-26** - (approved 2026-11-27) The agent lives inside the Phase 3 chat; layer-1 safety always runs before it.
- **2026-11-26** - (approved 2026-11-27) LLM for understanding only; dates are resolved by code, read-backs are fixed templates, and "yes" is decided by code.
- **2026-11-26** - (approved 2026-11-27) LangGraph 1.2.24 with `langgraph-checkpoint-sqlite` 3.1.3 in a separate checkpoint DB (resolves on Python 3.13, checked 2026-11-26).
- **2026-11-26** - (approved 2026-11-27) HITL uses the existing ApprovalService queue plus a new "approve with changes" (Edit) and a resume hook.
- **2026-11-27** - Plan approved by the user, including the open-question defaults:
  - patients can book through chat for any linked patient (`patient_ids`), as on the booking page;
  - the agent proposes 3 options;
  - a pending (awaiting-approval) booking keeps holding its slot.
- **2026-11-27** - **`.env.example` repaired.** The Phase 3 commit contained a hand-edited template: about a dozen settings were removed (`LLM_*`, `TRIAGE_*`, `DAILY_LLM_BUDGET_USD`, all `RAG_*`/`CHAT_*` lines) and `OPIK_WORKSPACE` was filled in with the workspace name. No API key was exposed (both key lines were empty). The template was rebuilt from Phase 2 + Phase 3 + Phase 4 settings with `OPIK_WORKSPACE` blank, and `tests/unit/test_config.py::test_env_example_lists_every_setting_and_holds_no_secrets` now fails CI if a setting goes missing or a secret line gets a value.
- **2026-11-27** - **Graph shape (deviation from 3.3/3.4).** One graph run = one patient turn (`ingest → route → collect | choose | confirm | list | abort | decided | resume → END`); the checkpointer (thread = chat session) carries the state between turns and across restarts. LangGraph `interrupt()` was dropped for the "ask the user" steps and the staff wait. Across separate HTTP requests an interrupted thread blocks new flows in the same chat: a patient waiting for approval could not start another booking. The HITL wait is instead persisted in `state.pending` and resolved by `BookingAgent.on_approval_decided` (a graph run with an `event`), which LangGraph checkpoints the same way. The per-request runtime (DB session, actor, LLM) goes in through LangGraph's `context`, so it is never checkpointed (verified: a new connection to the same SQLite file resumes the thread).
  - The read-back's approval preview reuses the policy via `BookingService.preview_approval`, which writes nothing. For reschedules the 24 h rule is judged by the *current* appointment's start, as the service does.
  - The keyword fallback passes clinical-sounding text as the visit reason, so the service's red-flag triage still runs when the LLM is down.
- **2026-11-27** - **T8-T11 notes:**
  - **ChatService integration:**
    - Signed-in *patients* get the agent; logged-out visitors keep the Phase 3 routing sentence and booking-page link.
    - While a flow is active, messages go straight to the agent (no router call), except questions: those are routed and answered by RAG with "We can continue your booking whenever you're ready."
    - Layer-1 emergencies pause the flow.
  - **Transactions:** the agent runs with the request's write transaction released and takes a fresh `BEGIN IMMEDIATE` right before a tool write. TriageService skips `outside_transaction` when already inside a savepoint (a booking tool), so it does not end the savepoint.
  - **Approval hooks are per app** (`app.state.approval_hooks`, passed into `ApprovalService`) rather than a global list, so test apps don't leak hooks into each other.
  - **Listing** ("what are my appointments") is also detected by code: the live eval showed the model sometimes labels it `none`.
  - **Evals:** the offline conversation gate uses keyword understanding (no LLM) over a fresh seeded DB copy per conversation, with scripted events (stolen slot, injected failure, front-desk decision, safety pause, time passing). The live suite uses the same conversations with `booking_nlu@v1`.
  - The pre-filled booking-page button now appears only on medical-question refusals (signed-in booking requests go to the agent).
