# Phase 1 Retro: clinic website, plus the Opik and MLOps foundation

## What shipped
- **REST API**: 46 routes covering auth, directory, scheduling, availability, appointments, approvals, front desk, doctor, admin, audit and notifications.
  - Every route has an explicit permission decision for the four roles and for anonymous callers, checked by `test_permission_matrix.py`.
  - Records the caller may not see return 404.
- **Auth**:
  - 15-minute JWTs, checked against the injected clock.
  - Rotating, hashed refresh tokens with reuse detection (the whole token family is revoked).
  - A login throttle.
  - Deactivated users are locked out immediately.
  - The app refuses to start outside dev with default secrets.
- **Booking**:
  - Race-safe. N=10 concurrent bookings of one slot give one 201 and nine 409s, with no 500s.
  - Idempotent. A replay returns the same body with `Idempotent-Replayed: true`, and reusing a key with a different body gives 422.
  - Duplicate and overlap checks.
  - The 24h boundary is tested at exactly 24h (free) and at 23h59m (needs approval).
  - Staff overrides require a reason, which is audited.
- **Approval queue (HITL)**:
  - Handles new bookings, reschedules and cancellations.
  - The original appointment stays untouched until a decision.
  - Optimistic locking prevents double decisions.
  - Requests over 4 hours old are flagged overdue.
  - Patients can withdraw their own requests.
- **Front desk and doctors**: today's queue, walk-ins, check-in, no-show (which flags the patient at 3), complete, guardian linking, and the doctor's own schedule.
- **Admin**:
  - Doctors, with forced deactivation.
  - Weekly blocks, leaves and holidays. Slot status is always re-derived from the generation rules, and the response lists affected bookings and notifies those patients.
  - Users, with last-admin and self-demotion guards.
  - Audit search.
- **Streamlit UI**: 14 role-based pages. They call the API only, keep tokens in session state, and send one idempotency key per form. Each page has an AppTest smoke test against the real API, plus booking and approval flow tests.
- **Opik and MLOps** (added at your request):
  - PII-safe tracing of the booking, approval and availability services. This was verified live in Opik Cloud: the trace held only IDs and statuses, and a phone number in the visit reason was dropped.
  - Eval harness with datasets, metrics, a runner, a regression gate and a CLI.
  - `booking_policy` (40 hand-written cases, 100%) and `safety_redflags_v0` (32 cases covering E1–E10 and U1–U6, schema-only for now).
  - A live Opik experiment was logged and read back: 40 items, both metrics 1.0.
  - CI `evals-offline` gate, plus an optional `evals-opik` upload job.

## Numbers
- **Tests**: 401 passing; 96% overall coverage, 97% for `services/`. ruff, `ruff format` and mypy `--strict` are clean (106 source files).
- **Clean install**: a fresh venv with `requirements.txt` and `requirements-dev.txt` passes the suite.
- **Manual demo**: run against a live uvicorn and Streamlit (agent API) on a scratch DB.
  - Register, then book a routine visit: confirmed and SMS queued.
  - Surgery consult: pending, then approved.
  - Leave added over a booking: 1 affected appointment and a `leave_conflict` notification queued.
  - Audit rows carry request IDs.

## What went well
- Putting the approval and ownership rules in the services rather than the routes means Phase 4's agent and Phase 6's MCP tools inherit them for free.
- The permission matrix built from OpenAPI caught two routes where body validation ran before the role check. Both were fixed by moving the role check into a route dependency.
- UI tests drive the real API through TestClient, so they found real integration problems that mocked tests would have missed.

## What was hard / surprises
- **SQLite WAL snapshot upgrades** return "database is locked" instead of waiting. Fixed with `BEGIN IMMEDIATE` for write requests.
- **The assertion session in tests** kept an old WAL read snapshot, so it missed the API's commits. The `fresh()` helper ends that read transaction.
- **A frozen test clock against a 15-minute token**: clock jumps expired the cached logins. The login fixture now logs in again automatically.
- **An expired idempotency key wasn't reusable**, because of the appointment-level unique key. A test found this real bug, and it is fixed.
- **Opik Cloud** needs a workspace, and the SDK ignores the constructor's `api_key`. Fixed by exporting Opik's own environment variables and looking up the default workspace.
- **Your Opik key had been pasted into `.env.example`**, which is committed to the public repo. It was moved to the git-ignored `.env` before any commit.

## Carry-overs
- ~~Verify `docker compose up` locally~~ **Dropped on 2026-11-18 (user decision):** this laptop can't run Docker because firmware virtualization is off. Local runs use plain Python, CI still builds the image, and Phase 7 deploys on Render's native Python runtime.
- The suite takes about 4 minutes, mostly scrypt logins and the seeded DB copy per test. Phase 7 could add a faster test password-hashing profile.
- The front desk can register patients without an account but cannot yet edit patient details. That is planned for Phase 7.
- `safety_redflags_v0` becomes a scored suite, with an emergency-recall gate, once Phase 2's classifier exists.
