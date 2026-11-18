# Phase 1 — Understanding (Clinic Website)

## Restated scope
From the brief, plus Phase 0 carry-overs:
1. **FastAPI:** auth (register/login/JWT/refresh), RBAC, CRUD for doctors/schedules/slots/appointments, and booking/cancel/reschedule endpoints with validation.
2. **Role dashboards (Streamlit):**
   - **Patient:** my appointments, book, cancel, reschedule
   - **Front desk:** today's queue, approve/reject/override, walk-ins, check-in/no-show
   - **Doctor:** my schedule, today's visit list, mark complete
   - **Admin:** doctors, schedules/leaves/holidays, users, audit log
3. **Booking safety:** concurrency-safe slot booking (partial unique index plus transaction), idempotency keys, protection against duplicate bookings.
4. **Tests:** unit and API tests for every endpoint and permission boundary, plus a real multi-threaded race test.
5. **Carry-over from Phase 0:** refuse to start with default secrets when `ENVIRONMENT != dev`.

**Out of scope:** any LLM feature (Phase 2+), chat UI (Phase 3), the agent's HITL interrupts (Phase 4 reuses the approval queue built here), real SMS/email (the outbox is simulated), accessibility polish (Phase 7 improvement #1; we use sensible defaults now).

## Key design decisions

### 1. Authentication
- **Access token:** JWT (HS256, `PyJWT`), 15 min. Claims: `sub` (user id), `role`, `jti`, `exp`, `iat`, `type=access`.
- **Refresh token:** a random opaque 256-bit token, stored **hashed** (sha256) in a new `refresh_tokens` table. It lasts 7 days and **rotates** on every refresh.
  - Reuse detection: if a revoked token is presented again, revoke that user's whole token family, since it was likely stolen.
- **Options considered:** stateless refresh JWTs (no revocation, rejected) vs DB-backed opaque tokens (revocable and auditable). **DB-backed chosen.**
- **Logout** revokes the presented refresh token.
- **Passwords:** keep stdlib scrypt from Phase 0. Minimum length 10. Login responses never reveal whether an email exists ("invalid credentials").
- **Login throttling:** in-process limiter of 5 failed attempts per email+IP per 15 min → 429. Simple and good enough for one process; Phase 7 moves it to a shared store if we scale out. Failed and successful logins are audited.
- **Startup guard:** if `ENVIRONMENT != dev` and `JWT_SECRET` or `SEED_DEFAULT_PASSWORD` still has its dev default, raise at app start.
- **Email validation:** registration uses `EmailStr`. Login accepts any string. Seeded staff use `@netracare.example`; there will be a test confirming `.example` addresses register and log in. If `email-validator` rejects them, we relax deliverability checks.

### 2. Authorisation (RBAC + ownership)
Two layers, both enforced:
- **Route layer:** `require_roles(*roles)` dependency → 403.
- **Service layer:** an ownership check on every resource read or write, so agents and MCP (Phase 4/6) inherit it.

| Role | Can see / do |
|---|---|
| patient | own patient profile + profiles they are a linked guardian for ("my patients"); appointments for those; book/cancel/reschedule for those |
| doctor | own schedule and own appointments (patient name + reason only); complete checked-in visits |
| front_desk | all appointments and patients; approve/reject; override; walk-ins; check-in; no-show; link guardians |
| admin | everything front desk can do, plus doctors, schedules, leaves, holidays, users, audit log |

- A patient accessing someone else's appointment gets **404, not 403**, so IDs can't be probed.
- The full permission matrix is a parametrised test.

### 3. Unified approval queue (HITL foundation)
- **Problem:** the state machine has no "cancel pending approval" state, and a <24h reschedule must not drop the old slot until staff agree.
- **Options:**
  - (a) Add more states to the appointment.
  - (b) Use a separate `approval_requests` table that references the appointment.
  - **(b) chosen.** One queue serves every exception kind, and the Phase 4 agent's HITL interrupt reuses it unchanged.
- **Table:** `approval_requests(id, kind ∈ {new_booking, reschedule, cancel}, appointment_id, target_appointment_id?, reasons JSON, status ∈ {pending, approved, rejected, withdrawn}, requested_by_user_id, decided_by_user_id?, decided_at?, decision_note?, created_at)`
- **New booking that needs approval:** the appointment is created as `pending_approval` and holds its slot(s). Approve → `confirmed`. Reject → `rejected`, slots released.
- **Reschedule:**
  - ≥24h (free): one transaction. Old → `rescheduled` (slots released), new → `confirmed` (or `pending_approval` if other reasons apply).
  - <24h: the new appointment is created as `pending_approval`, holding its new slot. The **old one stays `confirmed`**.
    - Approve → old `rescheduled`, new `confirmed`.
    - Reject → new `rejected`, old untouched.
- **Cancel:**
  - ≥24h: immediate.
  - <24h: an approval request is created and the appointment stays `confirmed`. Approve → `cancelled`. Reject → stays confirmed.
  - The patient sees "cancellation requested".
- **Withdrawn:** the patient can withdraw their own pending request.
- **Staleness:** the queue marks requests older than 4 h as overdue. The timeout UX for the agent is Phase 4.

### 4. Concurrency-safe booking
- **Database guard (source of truth):** the partial unique index `uq_active_slot_hold` from Phase 0. If two transactions try to hold the same slot, one gets `IntegrityError`, which maps to **409 `slot_unavailable`**.
- **Transaction:**
  1. Load and validate the slot(s): open, not past, belonging to that doctor, consecutive for 30-min types.
  2. Insert the appointment and its holds.
  3. Write the audit entry and outbox entry.
  4. Commit.
  - Any exception rolls back everything, so there are no partial writes.
- **Postgres readiness:** `SELECT … FOR UPDATE` on the slot rows (ignored on SQLite) narrows the race window. The index still decides.
- **SQLite specifics:** WAL mode + `busy_timeout=5000` (already set). The race test uses separate connections from a thread pool against a **file** DB.
- **Duplicate booking rule:** a patient cannot hold two active appointments that overlap in time with any doctor, or two active appointments with the same doctor on the same clinic-local day. Either → 409 `duplicate_booking`.

### 5. Idempotency keys
- **Header:** `Idempotency-Key` (8–64 chars). Required on `POST /appointments` and optional on cancel/reschedule.
- **Table:** `idempotency_records(key, user_id, method, path, request_hash, status_code, response_json, created_at)`, unique on `(user_id, key)`.
  - Same key + same body → replay the stored response with header `Idempotent-Replayed: true`.
  - Same key + different body → 422 `idempotency_key_reuse`.
  - Records expire after 24 h; cleanup happens lazily on write.
- The existing `appointments.idempotency_key` column is still filled, as a second guard.
- **Double-submit from the UI:** each booking form gets a key generated once per form render (stored in session_state), so a double click replays instead of double-booking.

### 6. Validation rules (booking)
| Rule | Error |
|---|---|
| Slot in the past (relative to `Clock`) | 422 `slot_in_past` |
| Slot blocked (leave) or doesn't exist | 409 `slot_unavailable` |
| Slot belongs to a different doctor | 422 `slot_doctor_mismatch` |
| 30-min type without 2 consecutive open slots | 422 `needs_consecutive_slots` |
| Doctor inactive | 409 `doctor_inactive` |
| Booking more than 8 weeks ahead (beyond the generated window) | 422 `too_far_ahead` |
| Patient not owned by caller | 404 |
| Minor booked by a non-guardian | 404 (no leak) |
| Duplicate/overlap | 409 `duplicate_booking` |
| Reason text longer than 500 chars | 422 |

Errors use a consistent shape: `{"error": {"code", "message", "details"}}`, plus `X-Request-ID` on every response.

### 7. Approval policy wiring
`BookingService` builds a `BookingContext` from the DB and calls `requires_approval()` from Phase 0:
- `is_new_patient` = no completed appointments yet.
- `patient_is_minor` uses `clinic_today`.
- `booked_by_guardian` = the caller is a guardian, not the patient.
- `red_flag_level` stays None in Phase 1; Phase 2 sets it.

**Staff override:** front desk/admin may pass `override=true` with a required `override_reason`. This skips the approval requirement (never the slot/duplicate checks) and is audited as `appointment.override`.

### 8. Front-desk operations
- **Today's queue:** today's (clinic-local) appointments grouped by doctor, with status, patient and approval flags, plus pending approvals (overdue highlighted).
- **Walk-in:** create a minimal patient (name, DOB, phone) if needed and book the next open slot today for the chosen doctor. Walk-ins are `routine`, auto-confirmed and audited.
- **Check-in:** `confirmed → checked_in`, allowed from 30 min before the slot until slot end.
- **No-show:** only after the slot ends. Increments `no_show_count` and re-evaluates `flagged`.
- **Complete:** doctor or staff, `checked_in → completed`.
- **Link guardian:** front desk links an existing patient to a guardian user (consent timestamp recorded).

### 9. Admin operations
- **Doctors:** create/update/deactivate. Deactivation fails if future active appointments exist unless `force=true`, which lists them.
- **Schedule templates:** add/remove. Slots are regenerated for the rolling window; removing a template blocks future open slots that no longer match. Booked slots are never touched; conflicts are reported instead.
- **Leaves:** add → block overlapping open slots and **return the affected active appointments** so front desk can contact those patients (each gets a `leave_conflict` notification in the outbox). Delete a leave → re-open the slots.
- **Holidays:** same pattern as leaves, applied to all doctors.
- **Users:** create staff users, change roles, deactivate (which also revokes their refresh tokens).
- **Audit log:** filter by entity, actor, action and date range; paginated.

### 10. Notifications (simulated)
`NotificationService.enqueue(patient_id, template, payload)` writes to the outbox in the same transaction.
- **Templates:** `booking_confirmed`, `booking_pending`, `booking_rejected`, `cancelled`, `cancel_requested`, `rescheduled`, `leave_conflict`.
- **Channel:** SMS if the patient has a phone, else email.
- A guardian receives notifications for their dependants.

### 11. Streamlit UI
- **UI ↔ API:** the UI talks **only** to the API over HTTP (`requests`, which Streamlit already depends on; pinned explicitly). A typed `ApiClient` wrapper handles the token, auto-refresh on 401, request IDs and error mapping.
- **Navigation:** `st.navigation` with pages chosen by role after login. Tokens are kept only in `st.session_state`, never in the URL.
- **Pages:**
  - Login / Register
  - Patient: My appointments, Book
  - Front desk: Today, Approvals, Walk-in, Patients
  - Doctor: My schedule
  - Admin: Doctors, Schedules & leave, Holidays, Users, Audit log
  - Shared: Notifications outbox
- **Accessibility baseline:** semantic headings, no colour-only status (text badges), larger base font in the theme. Full high-contrast/TTS is Phase 7.
- **Testing:** `streamlit.testing.v1.AppTest` smoke tests per page with a fake `ApiClient`.
- **Docker Compose:** add a `ui` service on 8501 with `API_BASE_URL`.

### 12. Time handling in the API
- Requests and responses use ISO-8601 with offset. Responses include both `start_utc` and `start_local` (clinic tz).
- Availability queries take clinic-local dates (`from_date`, `to_date`). The services convert using `Settings.tz`.
- `Clock` is injected via a FastAPI dependency (`get_clock`) so tests can freeze time.

### 13. Opik observability, evals & MLOps foundation (added at user request)
Phase 1 has no LLM calls, so this is the **foundation** that Phase 2+ plugs into. The full eval suite (LLM-as-judge, safety datasets at size, regression comparison in Opik) stays in Phase 5.
- **Tracing wrapper** `observability/tracing.py`: one `traced(name, tags)` decorator over `opik.track`. It is enabled only when `OPIK_API_KEY` is set and `opik_enabled=True`. Otherwise it is a zero-cost no-op, so tests and CI never need Opik.
- **PII-safe by construction:** every input, output and metadata payload goes through `redact()` before export. Only IDs are sent, never names, phones, emails or DOB, plus `request_id`, role, outcome and approval reasons. A test captures the exported payload with a fake client and asserts no PII.
- **Instrumented now:** `BookingService.book/cancel/reschedule`, `ApprovalService.approve/reject`, `AvailabilityService.find`. These give a real trace tree (request → service → outcome) before any LLM exists. Phase 2 adds LLM spans under the same request trace.
- **Eval harness** `evals/`:
  - a JSONL dataset format (`EvalCase(id, input, expected, tags, policy_ref)`)
  - a metric protocol
  - `run_experiment(dataset, task, metrics, name)`, which writes a local JSON report and, if Opik is enabled, uploads the dataset and logs an **Opik experiment** with run metadata (git SHA, dataset version, model names from Settings, prompt versions once they exist)
- **First datasets (deterministic, no LLM):**
  - `booking_policy` (~40 cases): booking context → expected approval reasons. Exercises `requires_approval`, the 24h window, no-show flags and minors.
  - `safety_redflags_v0` (~30 cases drawn from safety policy E1–E10 / U1–U6), with the expected red-flag level and route. It is only **schema-validated** now; Phase 2's classifier is scored against it.
- **MLOps / CI gate:**
  - `python -m netracare.evals run <dataset> --baseline evals/baselines/<dataset>.json --fail-under`
  - a CI job `evals-offline` runs the deterministic evals on every push, with no secrets needed, and fails on any metric regression against the committed baseline
  - an optional `evals-opik` job runs only on `main` when the `OPIK_API_KEY` repo secret exists and uploads the experiment
- **Run-config fingerprint:** a hash of the model names, judge settings and dataset version is attached to every experiment, so results are comparable across runs.

## Corner cases covered in Phase 1
- Two patients race for the same slot: exactly one 201, the other 409 (threaded test).
- Double-submit with the same idempotency key: one appointment, a replayed response.
- The same patient books two overlapping slots with different doctors: 409.
- A leave is added after booking: the appointment is flagged in the leave response and gets a `leave_conflict` notification; its slot hold is untouched.
- Booking a slot in the past or on a holiday or leave: rejected.
- Cancel/reschedule at exactly 24h (free) vs 23h59m (approval).
- A patient cancels another patient's appointment: 404. A doctor tries to approve: 403.
- A reschedule where the new slot is taken: 409, and the old appointment is unchanged.
- An approval is decided twice (two front-desk staff): the second gets 409 `already_decided` (optimistic `version`).
- A deactivated user's tokens stop working immediately (refresh revoked; the access token is rejected by an `is_active` check on each request).
- Empty, oversized and garbage inputs → 422 via Pydantic.
- A refresh token is reused after rotation: the whole token family is revoked.

## New dependencies
- Runtime: `pyjwt`, `email-validator`, `streamlit`, `requests`, `opik` (resolved 2.2.143 on Python 3.13 in Phase 0 probing).
- Dev: (none new expected).
- Verify `requirements.txt` with a clean install at the end, as in Phase 0.

## Risks
| Risk | Mitigation |
|---|---|
| SQLite write contention in race tests ("database is locked") | WAL + busy_timeout; the test asserts exactly one success *and* that every failure is a 409 (never a 500) |
| Streamlit version API drift (`st.navigation`, AppTest) | Load the `developing-with-streamlit` skill before UI work; pin the version |
| Scope size (API + 4 dashboards) | API first and fully tested; UI pages thin, calling the API only |
| Opik SDK network calls slow or flaky tests | Tracing is disabled unless a key is set; tests use a fake client; the CI upload job is optional and non-blocking |
| PII leaking into Opik traces | IDs only, mandatory `redact()`, and a test that inspects the exported payload |
| Approval-queue design might not fit Phase 4 | Kept generic (`kind`, `reasons`, `requested_by`) and services callable without HTTP |

## Open questions (defaults assumed)
- Should patients be allowed to add dependants themselves? **Assumed no.** Front desk links guardians, which matches the safety policy's consent rule.
- Do walk-ins need approval? **Assumed no** (staff-initiated).

## Decisions Log
- **2026-11-18** — Opaque, DB-backed, rotating refresh tokens with reuse detection; 15-min access JWT.
- **2026-11-18** — A unified `approval_requests` table (new_booking / reschedule / cancel) instead of extra appointment states; old appointment untouched until approval.
- **2026-11-18** — 404 (not 403) for other people's resources.
- **2026-11-18** — Generic `idempotency_records` table (per user + key, request hash, stored response).
- **2026-11-18** — UI uses `requests` against the API only; tokens only in session_state.
- **2026-11-18** — (User request) The Opik tracing, eval harness and CI eval gate move forward from Phase 5 to Phase 1 as a foundation. LLM-judge metrics and full datasets stay in Phase 5.

### Deviations recorded during execution
- **2026-11-18:** Errors live in `netracare/errors.py`, not `api/errors.py`. Services raise them too, and `api/errors.py` only maps them to JSON. `AppError(commit=True)` keeps side effects such as failed-login audit rows and token-family revocation.
- **2026-11-18:** SQLite write transactions use `BEGIN IMMEDIATE`. It is set per request in `get_db`, or with `session_scope(write=True)`, and is a no-op on Postgres. Without it, concurrent writers in WAL mode fail with "database is locked" when they try to upgrade a stale read snapshot. With it they queue, and the race test returns exactly one 201 and nine 409s. The partial unique index is still the last line of defence, tested in `tests/unit/services/test_service_guards.py`.
- **2026-11-18:** `Appointment.idempotency_key` stores `sha256(user_id:key)`, which gives a DB-level double-submit guard. When an idempotency record expires, this key is cleared so the client key becomes reusable. A test found this bug.
- **2026-11-18:** Reschedules only re-trigger the 24h rule, and only for patients. The other approval reasons (minor, surgery consult, no-show flag) were already checked when the original booking was confirmed.
- **2026-11-18:** Patients under 18 cannot self-register (422 `minor_self_registration`), in line with the consent rule.
- **2026-11-18:** Availability and booking share one rule: the slot start must be strictly after now.
- **2026-11-18:** Approval `created_at` comes from the injected clock, so the "overdue after 4h" rule is testable.
- **2026-11-18:** Role checks for book, complete and withdraw run as route dependencies. A forbidden role then gets 403 even when the request body is invalid, which the permission matrix relies on.
- **2026-11-18:** The permission matrix lists routes from `app.openapi()`. FastAPI 0.162 nests included routers, so `app.routes` no longer lists them.
- **2026-11-18:** The Opik SDK reads credentials reliably only from its own environment variables (`OPIK_API_KEY`, `OPIK_WORKSPACE`, …), so `apply_opik_env()` exports them. If `OPIK_WORKSPACE` is empty, the key's default workspace is looked up, which is what `opik configure` does.
- **2026-11-18:** The UI page folder is `ui/app_pages/` (Streamlit's current guidance), not `ui/pages/`. Navigation specs live in `ui/navigation.py`, which is unit-tested. UI tests run AppTest against the **real** API by passing FastAPI's TestClient to `ApiClient` as its HTTP transport, instead of using a fake client.
- **2026-11-18:** API fixtures moved to `tests/fixtures_api.py` and are registered once from a root `conftest.py`.
- **2026-11-18:** The app entrypoint is `uvicorn --factory netracare.api.main:app_factory`. There is no module-level app, so tests never touch the dev database.
