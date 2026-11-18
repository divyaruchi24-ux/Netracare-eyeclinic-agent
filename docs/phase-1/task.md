# Phase 1 — Tasks (exact changes)

Paths are relative to the repo root; the package is `src/netracare/`. Verify after each task group with `.\scripts\dev.ps1 check`.

## T0. Planning
- [x] `docs/phase-1/understanding.md`, `plan.md`, `task.md`
- [x] User approval: "Approve Phase 1 plan?"

## T1. Foundations
- [x] `uv add pyjwt email-validator streamlit requests`; `uv add --dev types-requests`.
- [x] `src/netracare/api/errors.py`
  - `class AppError(Exception)`: `status: int`, `code: str`, `message: str`, `details: dict | None`
  - subclasses: `NotFoundError(404)`, `ForbiddenError(403)`, `ConflictError(409)`, `ValidationAppError(422)`, `UnauthorizedError(401)`, `RateLimitedError(429)`
  - `def install_error_handlers(app: FastAPI) -> None`: `AppError`, `RequestValidationError`, `TransitionForbiddenError`→403, `InvalidTransitionError`→409, generic `Exception`→500 (no internals leaked) → JSON `{"error": {"code","message","details"}}`
- [x] `src/netracare/api/middleware.py`: `RequestIdMiddleware` (accepts inbound `X-Request-ID` if it matches `^[A-Za-z0-9-]{8,64}$`, else generates uuid4; sets `request_id_var`; echoes the header; logs method/path/status/duration).
- [x] `src/netracare/api/deps.py`
  - `get_engine()` / `get_session_factory()` (app.state), `def get_db(request) -> Iterator[Session]` (commit on success, rollback on error)
  - `def get_clock(request) -> Clock` (app.state.clock; tests override)
  - `def get_settings_dep() -> Settings`
- [x] `src/netracare/api/main.py`: `create_app(settings: Settings | None = None, clock: Clock | None = None, engine: Engine | None = None)`; registers middleware, error handlers, routers; calls `assert_safe_secrets(settings)`.
- [x] `src/netracare/config.py`: `def assert_safe_secrets(s: Settings) -> None` (raises `RuntimeError` when `environment != "dev"` and `jwt_secret`/`seed_default_password` equal their defaults or `len(jwt_secret) < 32`); add `access_token_minutes=15`, `refresh_token_days=7`, `api_base_url="http://localhost:8000"`, `cors_origins: list[str] = ["http://localhost:8501"]`.
- [x] `src/netracare/api/schemas/common.py`: `Page[T]` (items, total, limit, offset), `ErrorBody`, `LocalizedTime` helper (`start_utc`, `start_local`).
- [x] Tests `tests/api/test_foundations.py`: `test_request_id_generated_and_echoed`, `test_invalid_inbound_request_id_replaced`, `test_app_error_shape`, `test_validation_error_shape`, `test_unhandled_error_is_500_without_traceback`; `tests/unit/test_config.py::test_assert_safe_secrets_{dev_ok,prod_default_fails,prod_short_secret_fails,prod_strong_ok}`.
- [x] `tests/api/conftest.py`: fixtures `app` (migrated tmp DB + seeded with `TODAY=2026-11-10` via `Seeder`, `FixedClock(2026-11-10 09:00 IST)`), `client`, `login(email) -> headers`, `patient_headers`, `frontdesk_headers`, `doctor_headers(code)`, `admin_headers`, `assert_audited(session, action, entity_id)`.

## T2. Migration 0002
- [x] `src/netracare/domain/enums.py`: add `ApprovalKind(new_booking, reschedule, cancel)`, `ApprovalStatus(pending, approved, rejected, withdrawn)`.
- [x] `src/netracare/db/models.py`:
  - `RefreshToken(id, user_id FK, token_hash String(64) unique, family_id String(36) index, expires_at, revoked_at?, replaced_by_id?, created_at, user_agent?)`
  - `ApprovalRequest(IdMixin, TimestampMixin): kind, appointment_id FK, target_appointment_id FK?, reasons JSON, status, requested_by_user_id FK, decided_by_user_id FK?, decided_at?, decision_note Text?, version (version_id_col)`; index `(status, created_at)`; partial unique `uq_one_pending_per_appointment` on `appointment_id WHERE status='pending'`
  - `IdempotencyRecord(id, user_id FK, key String(64), method, path, request_hash String(64), status_code, response_json JSON, created_at)`; `UniqueConstraint(user_id, key)`
- [x] `alembic revision --autogenerate -m "auth approvals idempotency" --rev-id 0002`; review by hand.
- [x] Tests: the existing `test_models_match_migration` covers drift; add `tests/db/test_constraints.py::test_one_pending_approval_per_appointment`.

## T3. Auth
- [x] `src/netracare/security/tokens.py`
  - `def create_access_token(user_id: str, role: Role, settings, now: datetime) -> str`
  - `def decode_access_token(token: str, settings, now: datetime) -> AccessClaims` (raises `UnauthorizedError` on expired/invalid/wrong type)
  - `def new_refresh_token() -> tuple[str, str]` (raw, sha256 hash)
- [x] `src/netracare/security/rate_limit.py`: `class LoginThrottle(max_failures=5, window=timedelta(minutes=15))` with `check(key, now)`, `record_failure(key, now)`, `reset(key)`; thread-safe (Lock).
- [x] `src/netracare/repositories/token_repo.py`: `RefreshTokenRepository.add/by_hash/revoke/revoke_family/revoke_all_for_user`.
- [x] `src/netracare/services/auth_service.py` `AuthService(session, settings, clock, throttle)`:
  - `register_patient(cmd: RegisterPatient) -> TokenPair` (creates User(role=patient) + Patient(user_id, consent_captured_at=now); 409 `email_taken`)
  - `login(email, password, ip) -> TokenPair` (throttle; audit `auth.login`/`auth.login_failed`; generic 401)
  - `refresh(raw_token) -> TokenPair` (rotation; reuse → `revoke_family` + audit `auth.refresh_reuse` + 401)
  - `logout(raw_token) -> None`
- [x] `src/netracare/api/security.py`: `bearer = HTTPBearer(auto_error=False)`; `def current_user(...) -> CurrentUser` (decodes the token, loads the user, rejects inactive users); `def require_roles(*roles: Role) -> Callable`.
- [x] `src/netracare/api/schemas/auth.py`: `RegisterRequest(email: EmailStr, password: str = Field(min_length=10, max_length=128), full_name: str(1..128), dob: date, phone: str | None (pattern))`, `LoginRequest`, `RefreshRequest`, `TokenPair(access_token, refresh_token, token_type="bearer", expires_in)`, `MeResponse(user_id, email, role, display_name, patient_ids)`.
- [x] `src/netracare/api/routers/auth.py`: `POST /auth/register` 201, `POST /auth/login`, `POST /auth/refresh`, `POST /auth/logout` 204, `GET /auth/me`.
- [x] Tests `tests/api/test_auth.py`:
  - `test_register_creates_patient_and_returns_tokens`, `test_register_duplicate_email_409`, `test_register_weak_password_422`, `test_register_future_dob_422`
  - `test_login_ok_each_seeded_role`, `test_login_wrong_password_401_generic`, `test_login_unknown_email_401_same_message`, `test_login_throttled_after_5_failures_429`
  - `test_refresh_rotates`, `test_refresh_reuse_revokes_family`, `test_logout_revokes`
  - `test_expired_access_token_401`, `test_tampered_token_401`, `test_refresh_token_as_access_401`, `test_inactive_user_locked_out`
  - `test_me_patient_lists_own_and_dependants`
  - `test_example_tld_email_accepted`
- [x] Unit tests `tests/unit/security/test_tokens.py`, `test_rate_limit.py`.

## T4. Access policy + directory
- [x] `src/netracare/services/access.py` `AccessPolicy(session)`:
  - `patient_ids_for(user: CurrentUser) -> set[str]` (own + guardian links)
  - `assert_can_view_patient(user, patient_id)` → `NotFoundError`
  - `assert_can_view_appointment(user, appt)` (patient: owned patient; doctor: `appt.doctor_id == user.doctor_id`; staff: all)
  - `assert_can_act_for_patient(user, patient_id)`
- [x] `src/netracare/services/directory_service.py`: `list_doctors(specialty_code?, active_only)`, `get_doctor`, `create_doctor`, `update_doctor`, `deactivate_doctor(force)` (lists future active appointments → 409 `doctor_has_appointments` unless force), `list_specialties`; `search_patients(q, limit, offset)` (staff), `get_patient`, `link_guardian(patient_id, guardian_email, relationship)`, `create_walk_in_patient(...)`.
- [x] Routers `src/netracare/api/routers/directory.py`:
  - `GET /specialties`, `GET /doctors?specialty=&active=`, `GET /doctors/{id}` (public: no auth needed for directory reads)
  - `POST /doctors`, `PATCH /doctors/{id}` (admin)
  - `GET /patients?q=` (front_desk, admin), `GET /patients/{id}` (scoped), `POST /patients/{id}/guardians` (front_desk, admin)
- [x] Schemas `api/schemas/directory.py`: `DoctorOut(id, code, full_name, specialty{code,name}, bio_slug, is_active)`, `DoctorCreate/Update`, `PatientOut(id, full_name, dob, age, is_minor, phone_masked, email, no_show_count, flagged)`, `GuardianLinkIn`.
- [x] Tests `tests/api/test_directory.py` (list/filter/get/404, admin create/update/deactivate incl. `force`, patient search scoping, guardian link + audit) and `tests/unit/services/test_access.py`.

## T5. Scheduling admin
- [x] `src/netracare/services/schedule_service.py` `ScheduleService(session, clock, settings)`:
  - `list_templates(doctor_id)`, `add_template(doctor_id, weekday, start, end)` → regenerate, `remove_template(id)` → block now-unmatched future open slots
  - `regenerate_slots(doctor_id | None, weeks=8) -> int` (reuses `generate_slots` + `SlotRepository.upsert_many`)
  - `add_leave(doctor_id, start_local, end_local, reason) -> LeaveResult(leave, blocked_slots, affected_appointments)` (+ `leave_conflict` notifications)
  - `remove_leave(id) -> int` (re-opens slots not covered by another leave/holiday)
  - `add_holiday(day, name) -> HolidayResult` (blocks all doctors' slots that day + affected appointments), `remove_holiday(day)`
- [x] `src/netracare/repositories/slot_repo.py`: add `reopen_range(doctor_id, start, end) -> int`, `open_between(doctor_id, start, end) -> list[Slot]`, `active_hold_slot_ids(slot_ids) -> set[str]`.
- [x] Router `api/routers/schedule.py` (admin): `GET/POST /doctors/{id}/schedule-templates`, `DELETE /schedule-templates/{id}`, `GET/POST /doctors/{id}/leaves`, `DELETE /leaves/{id}`, `GET/POST /holidays`, `DELETE /holidays/{day}`, `POST /admin/slots/regenerate`.
- [x] Tests `tests/api/test_schedule.py`: `test_add_leave_blocks_slots_and_reports_affected`, `test_leave_conflict_notification_enqueued`, `test_remove_leave_reopens`, `test_remove_leave_keeps_slots_blocked_by_other_leave`, `test_holiday_blocks_all_doctors`, `test_add_template_generates_slots`, `test_remove_template_blocks_unmatched_future_slots_only`, `test_regenerate_idempotent`, `test_leave_end_before_start_422`, non-admin 403s.

## T6. Availability
- [x] `src/netracare/services/availability_service.py`:
  - `def find(doctor_id: str | None, specialty_code: str | None, from_date: date, to_date: date, appt_type: AppointmentType, now: datetime) -> list[AvailableOption]`
  - `AvailableOption(doctor_id, start_utc, end_utc, slot_ids: list[str])`; excludes past, blocked and held slots; 30-min types need consecutive slots of the same doctor; range ≤ 31 days; `from_date` clamped to today.
- [x] Router `api/routers/availability.py`: `GET /availability?doctor_id=&specialty=&from_date=&to_date=&type=` (any authenticated user). Response grouped by clinic-local date.
- [x] Tests `tests/unit/services/test_availability.py` (consecutive pairing across lunch: 12:45+13:00 must NOT pair; held slot excluded; past excluded; leave excluded) + `tests/api/test_availability.py` (range > 31 days → 422, unknown doctor → 404, needs either doctor or specialty → 422).

## T7. Booking core
- [x] `src/netracare/services/idempotency_service.py`: `IdempotencyService(session, clock)` `lookup(user_id, key, method, path, body) -> StoredResponse | None` (hash mismatch → `ValidationAppError("idempotency_key_reuse")`), `store(...)`, `purge_expired()`.
- [x] `src/netracare/services/notification_service.py`: `enqueue(patient_id, template, payload) -> NotificationOutbox` (channel selection), `list_for(user, limit, offset)`.
- [x] `src/netracare/services/audit_service.py`: thin wrapper `record(actor: CurrentUser | None, action, entity_type, entity_id, before, after, reason)` → `AuditRepository.append` (actor_type user/system).
- [x] `src/netracare/services/booking_service.py` `BookingService(session, clock, settings)`:
  - `book(actor, cmd: BookCommand) -> BookingResult` where `BookCommand(patient_id, doctor_id, slot_ids, type, reason, idempotency_key, override=False, override_reason=None)`; steps: access → validate slots (`_load_slots_for_update`) → duplicate/overlap check → `BookingContext` → `requires_approval` → insert appointment (+ `ApprovalRequest(new_booking)`) → holds → audit (`appointment.create`, `appointment.override`) → outbox → map `IntegrityError` to `ConflictError("slot_unavailable")`
  - `cancel(actor, appointment_id, reason) -> CancelResult` (≥24h immediate: `transition(CANCEL)` + `release_slots`; <24h & patient: `ApprovalRequest(cancel)`; staff: immediate)
  - `reschedule(actor, appointment_id, new_slot_ids, reason) -> BookingResult` (≥24h: single tx old→RESCHEDULED + new appointment with `rescheduled_from_id`; <24h & patient: new PENDING_APPROVAL + `ApprovalRequest(reschedule, target=new)`; new slot taken → 409, old untouched)
  - `check_in(actor, id)`, `mark_no_show(actor, id)` (after slot end; increments count, sets `flagged` via `should_flag_for_no_shows`), `complete(actor, id)` (doctor must own it)
  - private: `_is_new_patient(patient_id)`, `_assert_no_duplicate(patient_id, doctor_id, start, end, exclude_id=None)`, `_patient_age_flags(patient, actor)`
- [x] Schemas `api/schemas/appointments.py`: `BookRequest(patient_id, doctor_id, slot_ids: list[str] (1..2), type, reason: str | None (≤500), override: bool = False, override_reason: str | None)` with a validator (override needs a reason); `CancelRequest(reason)`, `RescheduleRequest(slot_ids, reason)`; `AppointmentOut(id, patient{id,name}, doctor{id,name,specialty}, type, status, start_utc, start_local, end_utc, reason, approval{id,kind,status,reasons} | None, rescheduled_from_id, created_at)`; `BookingResponse(appointment, requires_approval, approval_reasons)`.
- [x] Router `api/routers/appointments.py`:
  - `POST /appointments` (patient, front_desk, admin; `Idempotency-Key` header required) 201
  - `GET /appointments?patient_id=&doctor_id=&status=&from_date=&to_date=&limit=&offset=` (auto-scoped by role)
  - `GET /appointments/{id}`
  - `POST /appointments/{id}/cancel`, `POST /appointments/{id}/reschedule` (optional `Idempotency-Key`)
  - `POST /appointments/{id}/check-in`, `/no-show` (front_desk, admin), `POST /appointments/{id}/complete` (doctor, front_desk, admin)
- [x] Tests `tests/unit/services/test_booking_service.py` (service-level, FixedClock):
  - happy path auto-confirm; each approval reason → pending + ApprovalRequest; override skips approval + audited; override without reason 422
  - past slot / blocked / mismatched doctor / non-consecutive / inactive doctor / too far ahead
  - duplicate same doctor same day; overlap with another doctor
  - cancel ≥24h immediate & slot released; cancel 23h59m by patient → request; cancel by staff <24h → immediate
  - reschedule free path (single tx); <24h path keeps old confirmed; new slot taken → 409 & old unchanged
  - no-show before slot end 409; no-show increments + flags at 3; check-in window; doctor completes only own
  - every write emits audit + outbox (helper asserts)
  - rollback: simulate failure after insert (monkeypatch `NotificationService.enqueue` to raise) → no appointment, no holds, no audit row
- [x] Tests `tests/api/test_appointments.py`: endpoint-level coverage of the above, scoping (patient sees only own + dependants; doctor only own), 404 for someone else's appointment, missing Idempotency-Key → 422.
- [x] Tests `tests/api/test_idempotency.py`: `test_replay_returns_same_body_and_header`, `test_same_key_different_body_422`, `test_keys_are_per_user`, `test_expired_key_reusable`.
- [x] **Race test** `tests/integration/test_booking_race.py` (marker `db`): file DB, 10 distinct patients, `ThreadPoolExecutor(10)` + `threading.Barrier`, each with its own TestClient/session → assert `sorted(statuses) == [201] + [409]*9`, exactly 1 active hold, 1 appointment row with active hold, 0 × 500. Plus `test_double_submit_same_key_concurrent` → 1 appointment.

## T8. Approvals
- [x] `src/netracare/services/approval_service.py` `ApprovalService(session, clock)`:
  - `list(status=pending, overdue_after=timedelta(hours=4)) -> list[ApprovalView]` (with `is_overdue`)
  - `approve(actor, request_id, note) -> ApprovalView` (per kind: new_booking → CONFIRMED; reschedule → old RESCHEDULED + release, new CONFIRMED; cancel → CANCELLED + release) — all in one tx, audit + outbox
  - `reject(actor, request_id, note)` (new_booking → REJECTED + release; reschedule → new REJECTED + release, old untouched; cancel → stays CONFIRMED)
  - `withdraw(actor, request_id)` (requesting patient only; reschedule → new appointment CANCELLED + release)
  - `StaleDataError` / non-pending status → `ConflictError("already_decided")`
- [x] Router `api/routers/approvals.py`: `GET /approvals?status=` (front_desk, admin), `POST /approvals/{id}/approve`, `/reject` (front_desk, admin), `/withdraw` (patient owner).
- [x] Tests `tests/api/test_approvals.py`: each kind × approve/reject; withdraw; double-decide → 409; patient/doctor approve → 403; overdue flag with FixedClock +5h; notifications sent to patient/guardian.

## T9. Front desk + doctor ops
- [x] `src/netracare/services/front_desk_service.py`: `today_queue(date) -> QueueView` (by doctor, status counts, pending approvals count/overdue), `walk_in(actor, cmd: WalkInCommand(patient_id | new_patient{full_name,dob,phone}, doctor_id, reason)) -> BookingResult` (next open slot today from now; none → 409 `no_slots_today`).
- [x] `src/netracare/services/doctor_service.py`: `my_schedule(user, from_date, to_date) -> list[ScheduleDay]` (slots with status + appointment summary: patient name, type, reason, status).
- [x] Routers: `api/routers/front_desk.py` (`GET /front-desk/queue?date=`, `POST /front-desk/walk-ins`), `api/routers/doctor.py` (`GET /doctors/me/schedule?from_date=&to_date=` — doctor only).
- [x] Tests `tests/api/test_front_desk.py`, `tests/api/test_doctor.py`: queue contents/grouping; walk-in new + existing patient; walk-in when fully booked → 409; doctor sees only own schedule; non-doctor → 403.

## T10. Admin users, audit, notifications
- [x] `src/netracare/services/user_service.py`: `list_users(role?, q?)`, `create_staff(email, display_name, role, doctor_id?, temp_password)`, `update_user(id, role?, is_active?)` (deactivate → revoke all refresh tokens; cannot deactivate self; cannot remove the last admin → 409).
- [x] Routers: `api/routers/admin.py` — `GET/POST /users`, `PATCH /users/{id}`, `GET /audit-logs?entity_type=&entity_id=&actor_user_id=&action=&from=&to=&limit=&offset=` (admin); `api/routers/notifications.py` — `GET /notifications` (staff: all, filter `patient_id`; patient: own + dependants).
- [x] `src/netracare/repositories/audit_repo.py`: add `search(filters, limit, offset) -> tuple[list[AuditLog], int]` (read-only).
- [x] Tests `tests/api/test_admin.py`, `tests/api/test_notifications.py`.

## T11. Permission matrix
- [x] `tests/api/test_permission_matrix.py`: a table `ROUTES = [(method, path_template, {role: expected_status_class})]` covering **every** route registered in the app (assert the set of app routes == the set in the table, so new routes can't be untested) × {anonymous, patient, doctor, front_desk, admin}; expected 401/403/404/2xx.

## T12. Streamlit UI
- [x] Load the `developing-with-streamlit` skill before writing UI code.
- [x] `src/netracare/ui/api_client.py`: `class ApiClient(base_url, session_state)` with `login`, `register`, `refresh` (auto on 401 once), `logout`, `me`, `get/post/patch/delete` helpers, typed methods per endpoint used by pages; `class ApiError(Exception)(status, code, message, request_id)`; generates `Idempotency-Key` per form via `form_key()`.
- [x] `src/netracare/ui/app.py`: page config, theme, auth gate, `st.navigation` built from `pages_for(role)`; logout button; footer disclaimer (safety policy §10 chat footer wording, adapted).
- [x] `src/netracare/ui/components.py`: `status_badge(status)` (text + icon, not colour-only), `appointment_card(a, actions)`, `local_time(dt)`, `error_box(ApiError)` (shows the request ID).
- [x] Pages `src/netracare/ui/pages/`:
  - `login.py`, `register.py`
  - `patient_appointments.py` (upcoming/past, cancel/reschedule dialogs, pending-request banners, withdraw)
  - `patient_book.py` (choose patient → specialty/doctor → type → date range → options → confirm read-back → book)
  - `frontdesk_today.py` (queue by doctor; check-in / no-show / complete actions)
  - `frontdesk_approvals.py` (pending list, overdue first; approve/reject with note)
  - `frontdesk_walk_in.py`, `frontdesk_patients.py` (search, guardian link, book on behalf with override)
  - `doctor_schedule.py`
  - `admin_doctors.py`, `admin_schedule.py` (templates, leaves with affected-appointment table, holidays), `admin_users.py`, `admin_audit.py`
  - `notifications.py` (outbox viewer)
- [x] `.streamlit/config.toml` (theme: base font size, high-contrast-friendly palette).
- [x] `scripts/dev.ps1` + `Makefile`: add `ui` task (`streamlit run src/netracare/ui/app.py`).
- [x] `docker-compose.yml`: `ui` service (same image, `streamlit run …`, port 8501, `API_BASE_URL=http://api:8000`, depends_on api healthy).
- [x] Tests `tests/ui/test_pages.py` (AppTest): `FakeApiClient` fixture; for each role, login → expected nav entries; each page renders without exception with fake data; booking page read-back step shows the exact doctor/date/time before the confirm button; error box shows the request ID.

## T12b. Opik observability, evals & MLOps foundation
- [x] `uv add opik`; `config.py`: add `opik_enabled: bool = False`, `opik_url_override: str | None = None`; update `.env.example`.
- [x] `src/netracare/observability/tracing.py`
  - `def configure_tracing(settings) -> bool` (calls `opik.configure` only when enabled + key present; never raises at startup; logs `tracing.disabled` otherwise)
  - `def traced(name: str, *, tags: list[str] | None = None) -> Callable` (wraps `opik.track` when enabled; else returns the function unchanged)
  - `def safe_payload(obj) -> Any` (recursively applies `redact()`; drops keys in `PII_KEYS = {full_name, phone, email, dob, password, token}`)
  - `def set_trace_metadata(**kw)` (request_id, role, outcome)
- [x] Instrument: `BookingService.book/cancel/reschedule`, `ApprovalService.approve/reject`, `AvailabilityService.find`; call `configure_tracing` in `create_app`.
- [x] `src/netracare/evals/`
  - `schema.py`: `EvalCase(id, input: dict, expected: dict, tags: list[str], policy_ref: str | None)`, `load_dataset(path) -> list[EvalCase]` (duplicate IDs → error)
  - `metrics.py`: `Metric` Protocol `(name, score(case, output) -> float)`; `ExactMatch`, `SetEquality`, `Recall(label)`, `Accuracy`
  - `runner.py`: `run_experiment(dataset_name, task, metrics, *, experiment_name, settings) -> ExperimentReport` (local JSON in `reports/evals/`; when enabled → `client.get_or_create_dataset` + `opik.evaluation.evaluate` with metadata `{git_sha, dataset_version, config_fingerprint}`)
  - `gate.py`: `compare(report, baseline, tolerance=0.0) -> list[Regression]`
  - `__main__.py`: `python -m netracare.evals run <dataset> [--baseline PATH] [--fail-under] [--upload]`, `... update-baseline <dataset>`
  - `tasks.py`: `booking_policy_task(case) -> dict` (builds a `BookingContext` → `requires_approval`)
- [x] Datasets `evals/datasets/booking_policy.jsonl` (~40 cases, incl. 24h boundary, 3 no-shows, minor without guardian, surgery consult, new patient) and `evals/datasets/safety_redflags_v0.jsonl` (~30 cases mapped to E1–E10 / U1–U6, plus benign controls; schema-validated only).
- [x] Baseline `evals/baselines/booking_policy.json`; `reports/` git-ignored.
- [x] `.github/workflows/ci.yml`: job `evals-offline` (run + `--fail-under` against the baseline); job `evals-opik` (`if: github.ref == 'refs/heads/main' && secrets.OPIK_API_KEY != ''`, `continue-on-error: true`, `--upload`).
- [x] `scripts/dev.ps1` + `Makefile`: `evals` task.
- [x] Tests `tests/unit/observability/test_tracing.py`: `test_traced_is_noop_when_disabled`, `test_configure_without_key_disabled`, `test_exported_payload_has_no_pii` (fake Opik client captures payload), `test_tracing_failure_never_breaks_booking`. `tests/unit/evals/`: `test_load_dataset_rejects_duplicate_ids`, `test_metrics`, `test_gate_detects_regression`, `test_runner_writes_local_report`, `test_booking_policy_dataset_passes_100pct`, `test_safety_dataset_schema_and_coverage` (every E/U code has ≥1 case), `test_cli_fail_under_exit_code`.
- [x] `docs/evals.md`: how to run evals locally, view them in Opik and update baselines.

## T13. Close-out
- [x] `.\scripts\dev.ps1 requirements` → verify pins; clean venv install of `requirements.txt` + `requirements-dev.txt` + pytest.
- [x] `ruff check`, `ruff format --check`, `mypy`, `pytest --cov` (≥90% overall, ≥95% `services/`).
- [x] Manual demo script from `plan.md` (API + UI running locally).
- [x] `docs/phase-1/RETRO.md`; tick boxes here + global `docs/task.md`; Decisions Log deviations.
- [x] Commit `phase-1: clinic website - auth, RBAC, booking, approvals, dashboards`; push; CI green.
