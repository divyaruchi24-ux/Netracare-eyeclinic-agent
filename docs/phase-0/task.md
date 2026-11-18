# Phase 0 — Tasks (exact changes)

Paths relative to repo root. Package root: `src/netracare/`.

## T0. Planning
- [x] `docs/understanding.md`, `docs/plan.md`, `docs/task.md`
- [x] `docs/phase-0/understanding.md`, `plan.md`, `task.md`
- [x] User approval: "Approve Phase 0 plan?"

## T1. Tooling bootstrap
- [x] Install uv: `python -m pip install --user uv` (verify `uv --version`). *(uv 0.13.7; invoked as `python -m uv`)*
- [x] `git init`; create `.gitignore` (Python, `.venv*/`, `.env`, `*.db`, `chroma/`, `.mypy_cache/`, `.ruff_cache/`, `.pytest_cache/`, `htmlcov/`, `.opik/`).
- [x] `.python-version` → `3.13` (or `3.12` if T1.5 probe fails).
- [x] `pyproject.toml`:
  - `[project] name="netracare"`, `requires-python=">=3.11"`, Phase 0 runtime deps.
  - `[dependency-groups] dev = [...]`.
  - `[tool.hatch]`/`[build-system]` hatchling with `packages = ["src/netracare"]`.
  - `[project.scripts] netracare-seed = "netracare.seed.__main__:main"`.
- [x] T1.5 Compatibility probe: `uv pip compile` a scratch `probe.in` containing `chromadb sentence-transformers rank-bm25 opik langgraph langchain-groq groq mcp` for the chosen Python; record pass/fail in phase understanding Decisions Log. Do **not** add them to the project.
- [x] `uv sync` → creates `.venv` + `uv.lock`.

## T2. Quality tooling
- [x] `pyproject.toml` `[tool.ruff]` (line-length 100, target py311, select `E,F,I,B,UP,SIM,RUF,N,S` w/ `S101` ignored in tests), `[tool.ruff.format]`.
- [x] `[tool.mypy]` strict-ish: `strict = true`, `plugins = ["pydantic.mypy"]`, `mypy_path = "src"`.
- [x] `[tool.pytest.ini_options]`: `testpaths=["tests"]`, `addopts="-q --strict-markers"`, markers `live`, `db`.
- [x] `.pre-commit-config.yaml`: ruff (check + format), mypy (local hook), end-of-file-fixer, trailing-whitespace, check-yaml, detect-private-key.
- [x] `Makefile` targets: `install, lint, fmt, type, test, check, migrate, seed, api, requirements`.
- [x] `scripts/dev.ps1` with the same task names (switch on `$args[0]`).

## T3. Config & infra
- [x] `src/netracare/__init__.py` (`__version__ = "0.1.0"`).
- [x] `src/netracare/config.py`:
  - `class Settings(BaseSettings)`: fields listed in phase understanding §Config; `model_config = SettingsConfigDict(env_file=".env", extra="ignore")`.
  - `def get_settings() -> Settings` (lru_cache).
- [x] `.env.example` with every Settings key, placeholder values, comments; includes:
  ```
  GROQ_API_KEY=
  GROQ_MODEL_REASONING=openai/gpt-oss-120b
  GROQ_MODEL_FAST=openai/gpt-oss-20b
  JUDGE_PROVIDER=groq
  JUDGE_MODEL=qwen/qwen3.8-27b
  JUDGE_REASONING_EFFORT=none
  JUDGE_MAX_TOKENS=900
  OPIK_API_KEY=
  OPIK_WORKSPACE=
  OPIK_PROJECT_NAME=netracare
  ```
- [x] `src/netracare/logging.py`: `configure_logging(level: str) -> None` (structlog JSON, `request_id` contextvar), `redact(value: str) -> str` placeholder (emails/phones regex) — full redaction in Phase 2.
- [x] `src/netracare/api/main.py`: `create_app() -> FastAPI`, `GET /health -> {"status": "ok", "version": ...}`; `app = create_app()`.
- [x] `Dockerfile` (python:3.13-slim (or 3.12), uv install, non-root user, `uvicorn` CMD). *(written; build/run not verified locally — Docker not installed; CI job added)*
- [x] `docker-compose.yml`: services `api` (8000), `ui` (8501, placeholder command until Phase 1), volume for `data/`.
- [x] `.github/workflows/ci.yml`: on push/PR → setup uv → `uv sync --frozen` → ruff check + format check → mypy → pytest; second job installs from `requirements.txt` to verify it.
- [x] Test: `tests/api/test_health.py::test_health_returns_ok`.

## T4. Domain (pure)
- [x] `src/netracare/domain/enums.py`: `Role`, `AppointmentStatus`, `AppointmentEvent`, `AppointmentType` (`routine_15`, `new_patient_30`, `surgery_consult_30`, `follow_up_15`), `SlotStatus`, `DocType`, `ActorType`, `ApprovalReason`; `ACTIVE_STATUSES: frozenset`, `TERMINAL_STATUSES: frozenset`.
- [x] `src/netracare/domain/clock.py`: `class Clock(Protocol): def now_utc(self) -> datetime`; `SystemClock`; `FixedClock(at: datetime)`; `def to_clinic_local(dt, tz) -> datetime`; `def clinic_today(clock, tz) -> date`.
- [x] `src/netracare/domain/state_machine.py`:
  - `TRANSITIONS: dict[tuple[AppointmentStatus, AppointmentEvent], tuple[AppointmentStatus, frozenset[Role | Literal["system"]]]]`
  - `class InvalidTransition(Exception)`, `class TransitionForbidden(Exception)`
  - `def transition(current: AppointmentStatus, event: AppointmentEvent, actor: Role | Literal["system"]) -> AppointmentStatus`
- [x] `src/netracare/domain/policies.py`:
  - `CHANGE_WINDOW = timedelta(hours=24)`, `NO_SHOW_FLAG_THRESHOLD = 3`
  - `def is_within_change_window(appt_start_utc: datetime, now_utc: datetime) -> bool` (True if `< 24h`)
  - `@dataclass(frozen=True) class BookingContext` (is_change, appt_start_utc, now_utc, appointment_type, is_new_patient, patient_is_minor, booked_by_guardian, red_flag_level, patient_no_show_flagged)
  - `def requires_approval(ctx: BookingContext) -> list[ApprovalReason]`
  - `def is_minor(dob: date, today: date) -> bool`
- [x] `src/netracare/domain/slot_generation.py`:
  - `@dataclass(frozen=True) class WorkBlock(weekday: int, start: time, end: time)`
  - `@dataclass(frozen=True) class SlotSpec(doctor_id: str, start_utc: datetime, end_utc: datetime)`
  - `def generate_slots(doctor_id, blocks: Sequence[WorkBlock], leaves: Sequence[tuple[datetime, datetime]], holidays: set[date], start: date, days: int, tz: ZoneInfo, slot_minutes: int = 15, lunch: tuple[time, time] = (time(13), time(14))) -> list[SlotSpec]`
- [x] Tests (`tests/unit/domain/`):
  - `test_state_machine.py`: `test_valid_transitions_table` (parametrized over TRANSITIONS), `test_illegal_transition_raises`, `test_wrong_role_forbidden`, `test_terminal_states_have_no_outgoing`
  - `test_policies.py`: `test_exactly_24h_is_free`, `test_23h59m_requires_approval`, `test_auto_confirm_when_no_reasons`, parametrized test per `ApprovalReason`, `test_is_minor_birthday_boundary_in_ist`
  - `test_slot_generation.py`: `test_skips_sunday`, `test_skips_holiday`, `test_skips_lunch`, `test_respects_leave_partial_overlap`, `test_slots_are_utc_and_15_min`, `test_deterministic_output`, `test_non_utc_dst_zone_correct` (America/New_York across DST)
  - `test_clock.py`: `test_fixed_clock`, `test_clinic_today_near_midnight_utc`

## T5. Persistence
- [x] `src/netracare/db/base.py`: `class Base(DeclarativeBase)`, `uuid_pk()` helper, `TimestampMixin(created_at, updated_at)`.
- [x] `src/netracare/db/session.py`: `make_engine(url) -> Engine` (SQLite: `foreign_keys=ON`, WAL, `check_same_thread=False`), `SessionLocal`, `@contextmanager session_scope()`.
- [x] `src/netracare/db/models.py` ORM classes:
  - `User(id, email unique, password_hash, role, is_active)`
  - `Specialty(id, code unique, name, description)`
  - `Doctor(id, code unique, full_name, specialty_id FK, bio_slug, is_active)`
  - `StaffUser(id, user_id FK unique, display_name, doctor_id FK nullable)`
  - `Patient(id, user_id FK nullable, full_name, dob, phone, email, no_show_count, flagged, consent_captured_at)`
  - `PatientGuardian(patient_id FK, guardian_user_id FK, relationship, consent_captured_at)` PK composite
  - `ScheduleTemplate(id, doctor_id FK, weekday, start_local, end_local, valid_from, valid_to)`
  - `Leave(id, doctor_id FK, start_utc, end_utc, reason)`
  - `Holiday(date PK, name)`
  - `Slot(id, doctor_id FK, start_utc, end_utc, status)`; `UniqueConstraint(doctor_id, start_utc)`; index `(doctor_id, start_utc)`
  - `Appointment(id, patient_id FK, doctor_id FK, type, status, reason_text, booked_by_user_id FK, idempotency_key unique nullable, rescheduled_from_id FK self nullable, approval_reasons JSON, red_flag_level nullable, version int for optimistic locking)`
  - `AppointmentSlot(appointment_id FK, slot_id FK, active bool)`; `Index("uq_active_slot_hold", "slot_id", unique=True, sqlite_where=active, postgresql_where=active)`
  - `AuditLog(id, at_utc, actor_user_id, actor_type, action, entity_type, entity_id, before_json, after_json, request_id, reason)`
  - `KnowledgeDoc(id, slug, title, doc_type, version, effective_date, superseded_by_id, source_path, checksum)`; unique `(slug, version)`
  - `NotificationOutbox(id, channel ∈ {sms,email}, to_patient_id, template, payload_json, status, created_at, sent_at)`
- [x] Alembic: `alembic.ini`, `src/netracare/db/migrations/env.py` (reads `get_settings().database_url`, `render_as_batch=True` for SQLite), `versions/0001_initial.py` (autogenerate then hand-edit to add audit triggers via `op.execute` for SQLite dialect: `BEFORE UPDATE`/`BEFORE DELETE ON audit_logs → RAISE(ABORT, 'audit log is append-only')`).
- [x] `src/netracare/repositories/`: `base.py` (`Repository[T]` generic w/ session), `slot_repo.py` (`upsert_many(specs) -> int`, `block_range(doctor_id, start, end) -> int`), `appointment_repo.py` (`hold_slots(appt, slot_ids)`), `audit_repo.py` (`append(entry) -> AuditLog`; **no update/delete**), `doctor_repo.py`, `patient_repo.py`, `knowledge_repo.py`.
- [x] Tests (`tests/db/`, marker `db`, fixture: tmp SQLite file + `alembic upgrade head`):
  - `test_migrations.py::test_upgrade_and_downgrade`
  - `test_constraints.py::test_two_active_holds_on_same_slot_rejected`, `::test_inactive_hold_allows_rebook`, `::test_slot_unique_per_doctor_start`
  - `test_audit_append_only.py::test_update_blocked`, `::test_delete_blocked`
  - `test_slot_repo.py::test_upsert_is_idempotent`, `::test_block_range_blocks_overlapping_slots`

## T6. Seed data & KB
- [x] `seed/data/specialties.yaml`, `doctors.yaml` (5 doctors + weekly blocks), `holidays.yaml`, `leaves.yaml` (relative offsets from seed date), `staff.yaml`.
- [x] `src/netracare/seed/__init__.py`, `__main__.py` (`main(argv) -> int`; flags `--reset`, `--weeks 8`, `--summary`), `loaders.py` (`load_yaml(path) -> dict`), `patients.py` (`make_patients(n=30, seed=42, today) -> list[PatientSeed]` via Faker `en_IN`, fake phones `+91 90000 0xxxx`, `@example.com`; 5 minors w/ guardians, 3 flagged no-show, 1 elderly w/ adult-child guardian), `kb.py` (scan `kb/**/*.md`, parse front-matter, upsert `KnowledgeDoc` with checksum).
- [x] Passwords for seeded users from `SEED_DEFAULT_PASSWORD` env, hashed with a placeholder `hash_password` (`hashlib.scrypt` in `src/netracare/security/passwords.py`; Phase 1 may swap to argon2/bcrypt).
- [x] `kb/` markdown stubs with front-matter (`slug, title, doc_type, version, effective_date, doctor_code?, procedure?`):
  - `kb/doctors/*.md` ×5, `kb/procedures/{lasik,cataract,glaucoma-screening,dilated-exam,diabetic-eye-screening,squint-evaluation,dry-eye,ptosis}.md`
  - `kb/pricing/price_list_2025.md` (v1, eff. 2025-04-01) and `price_list_2026.md` (v2, eff. 2026-04-01, LASIK price changed)
  - `kb/insurance/accepted_insurers.md`, `kb/prep/{dilation,surgery-fasting,contact-lens-pause}.md`, `kb/policies/{cancellation,privacy-summary}.md`, `kb/clinic/{hours-location,faq-general}.md`
  - Footer on each: "Clinic-approved general information. Not medical advice."
- [x] Tests (`tests/seed/`): `test_seed_counts` (5 doctors, 5 specialties, ≥30 patients, staff 8), `test_seed_idempotent`, `test_minors_have_guardians`, `test_no_slots_on_holidays_or_leave`, `test_kb_front_matter_valid`, `test_price_list_v1_superseded_by_v2`.

## T7. Safety policy
- [x] `docs/safety_policy.md` sections:
  1. Purpose & scope; 2. Allowed content (logistics, cited KB education, routing w/ non-diagnostic phrasing); 3. Forbidden content (diagnosis, interpreting readings/lab results/prescriptions, drug/dose/drop recommendations, stopping/changing meds, prognosis, comparing doctors' competence, other patients' data); 4. **Red-flag symptom list** (sudden vision loss, curtain/shadow, flashes + new floaters, chemical splash, penetrating/blunt trauma, severe pain + nausea/halos/red eye, sudden double vision, eye pain after recent surgery, contact-lens wearer with severe pain/redness, newborn/child with white pupil reflex — categorized emergency vs urgent); 5. **Escalation script** (exact text, emergency number 112, clinic emergency line placeholder, chemical splash → rinse with clean water immediately while seeking care — the only first-aid line allowed and it is policy-approved), 6. Non-diagnostic routing phrasing templates; 7. Third-party/minor consent rules; 8. Lab/prescription handling; 9. Prompt-injection & data-access rules; 10. Disclaimer texts (chat footer, KB answer, summary draft, lab notification); 11. Logging/PII rules; 12. Change control (policy version + date).

## T8. Close-out
- [x] `uv export --no-dev --no-hashes --format requirements-txt -o requirements.txt`; `uv export --only-group dev --no-hashes --format requirements-txt -o requirements-dev.txt` (ensure `==` pins).
- [x] Clean install check: `python -m venv .venv-check; .venv-check\Scripts\pip install -r requirements.txt -r requirements-dev.txt; .venv-check\Scripts\pip install -e . --no-deps`; delete `.venv-check` after.
- [x] `ruff check .`, `ruff format --check .`, `mypy src`, `pytest --cov=netracare.domain --cov-report=term-missing` all pass.
- [x] Manual: `alembic upgrade head`, `python -m netracare.seed --summary`, `uvicorn` + `/health`.
- [x] `docs/phase-0/RETRO.md`.
- [x] Tick boxes here + global `docs/task.md`; append Decisions Log deviations.
- [x] `git add -A; git commit -m "phase-0: foundations, domain model, seed data, safety policy"`.
