# Phase 0 — Understanding (Foundations & Domain Modeling)

## Restated scope
1. Repo scaffold with `src/netracare` layout, uv project, **pinned `requirements.txt` (mandatory)** + `requirements-dev.txt`, ruff, mypy, pre-commit, Dockerfile + Docker Compose, GitHub Actions CI (lint + tests), `.env.example`, `Makefile` + `scripts/dev.ps1`.
2. Domain model + SQLAlchemy ORM + Alembic initial migration: Patient, Doctor, Specialty, ScheduleTemplate, Leave, Holiday, Slot, Appointment (+ AppointmentSlot), StaffUser, AuditLog, KnowledgeDoc, NotificationOutbox.
3. Pure domain logic: appointment state machine, booking policy (24h window, approval rules), slot generation.
4. Seed data: 5 doctors, weekly schedules, leaves/holidays, ~30 fake patients (incl. minors with guardians, some no-show history), staff accounts, fictional price list (2 versions) + FAQs as KB markdown stubs.
5. `docs/safety_policy.md`.

**Out of scope for Phase 0:** API endpoints (beyond `/health`), auth logic, UI pages, any LLM call. (Phase 0 only adds the *config* placeholders for Groq/Opik.)

## Dependencies for Phase 0 only
Runtime: `sqlalchemy`, `alembic`, `pydantic`, `pydantic-settings`, `fastapi`, `uvicorn[standard]`, `python-dotenv`, `structlog`, `faker`, `pyyaml`, `tzdata` (Windows needs it for `zoneinfo`).
Dev: `pytest`, `pytest-cov`, `freezegun` (or `time-machine`), `ruff`, `mypy`, `pre-commit`, `types-PyYAML`.

LLM/RAG/eval deps (`groq`, `langchain-groq`, `langgraph`, `chromadb`, `sentence-transformers`, `rank-bm25`, `opik`, `mcp`) are **not** added until their phase, which keeps the Phase 0 install fast and lets each phase verify its own `requirements.txt` delta. *Exception considered:* do a throwaway compatibility probe now (`uv pip install --dry-run chromadb sentence-transformers opik langgraph langchain-groq` on 3.13) so we know early whether to pin Python 3.12. **Chosen: do the dry-run probe in Phase 0 and record the result; don't add the deps.**

## Design options & trade-offs

### Python version
- 3.13 is installed. Risk: heavy ML deps. → dry-run probe (above). If it fails: `uv python install 3.12` and pin `requires-python = ">=3.11,<3.13"`. `.python-version` file records the choice.

### IDs
- Integer autoincrement (simple, SQLite-friendly) vs UUID (non-enumerable, safer to expose). **Chosen: UUIDv4 stored as `String(36)`** for patient/appointment public IDs (prevents ID enumeration across patients), integer PKs are not exposed. Simpler: **UUID string PK everywhere** — one convention, Postgres-portable. Chosen.

### Users vs Patients vs Staff
- Single `users` table with role + separate `patients` / `staff_profiles`? Brief names `Patient` and `StaffUser` separately.
- **Chosen:** `users` (id, email, password_hash, role ∈ {patient, front_desk, doctor, admin}, is_active) for auth; `patients` (profile, DOB, phone, guardian link, no_show_count, flagged) with optional `user_id` (a guardian's user can manage several patient profiles); `staff_users` is a profile table keyed to `users` with optional `doctor_id` for doctor role. Keeps auth uniform for JWT in Phase 1.

### Guardian / third-party model
- `patients.guardian_patient_id` is too restrictive (guardian may not be a patient). **Chosen:** `patient_guardians(patient_id, guardian_user_id, relationship, consent_captured_at)`; minors (age < 18 at clinic-local today) require at least one guardian link to book via the agent.

### Slots & double-booking prevention
- `slots(id, doctor_id, start_utc, end_utc, status ∈ {open, blocked})`, unique `(doctor_id, start_utc)`.
- `appointment_slots(appointment_id, slot_id, active bool)` + **partial unique index `ON appointment_slots(slot_id) WHERE active`** — supported by both SQLite (≥3.8) and Postgres. Cancel/reject/reschedule set `active=false` in the same transaction as the status change.
- Slot generation: `generate_slots(doctor, template, leaves, holidays, start_date, weeks) -> list[SlotSpec]` is a pure function; repository upserts idempotently (re-running the seed doesn't duplicate).
- Leaves added after slots exist → slots in the range set to `blocked`; existing appointments in that range are surfaced for staff (Phase 1 service).

### Appointment status machine (final)
States: `requested, pending_approval, confirmed, checked_in, completed, cancelled, rejected, no_show, rescheduled`.

| From | Event | To | Allowed actors |
|------|-------|----|----|
| requested | submit (needs approval) | pending_approval | system |
| requested | submit (auto-approve) | confirmed | system |
| pending_approval | approve | confirmed | front_desk, admin |
| pending_approval | reject | rejected | front_desk, admin |
| pending_approval | cancel | cancelled | patient(owner), front_desk, admin |
| confirmed | cancel | cancelled | patient(owner, policy-checked), front_desk, admin |
| confirmed | reschedule | rescheduled | patient(owner, policy-checked), front_desk, admin |
| confirmed | check_in | checked_in | front_desk, admin |
| confirmed | mark_no_show | no_show | front_desk, admin (only after slot end) |
| checked_in | complete | completed | doctor, front_desk, admin |

Terminal: completed, cancelled, rejected, no_show, rescheduled. Active (hold slots): requested, pending_approval, confirmed, checked_in.
Ownership/time checks live in the **policy** layer, not the state table; state table only checks role + legality.

### Approval policy (pure function)
`requires_approval(ctx) -> list[ApprovalReason]` where reasons ∈ {`within_24h_change`, `new_patient_surgery_consult`, `guardian_booking_minor`, `post_red_flag`, `no_show_flagged`}. Empty list → auto-confirm.

### Audit log
Append-only: `audit_logs(id, at_utc, actor_user_id|null, actor_type ∈ {user, agent, system}, action, entity_type, entity_id, before_json, after_json, request_id, reason)`. No update/delete methods in the repository; a SQLite trigger blocks UPDATE/DELETE (Postgres equivalent added in a later migration if we switch). PII minimization: before/after store IDs and status fields, not free-text clinical content.

### KnowledgeDoc
`knowledge_docs(id, slug, title, doc_type ∈ {doctor_bio, procedure_faq, price_list, insurance, prep_instructions, policy, general_faq}, version, effective_date, superseded_by_id, source_path, checksum)`. Phase 0 seeds metadata + markdown files in `kb/`; chunking/indexing is Phase 3.

### Clock abstraction
`Clock` protocol with `now_utc()`; `SystemClock` and `FixedClock` (tests). `clinic_tz = ZoneInfo(settings.clinic_timezone)`. `tzdata` dependency required on Windows.

### Config
`pydantic-settings` `Settings` reading `.env`: `DATABASE_URL`, `JWT_SECRET`, `CLINIC_TIMEZONE`, `GROQ_API_KEY`, `GROQ_MODEL_REASONING`, `GROQ_MODEL_FAST`, `JUDGE_PROVIDER`, `JUDGE_MODEL`, `JUDGE_REASONING_EFFORT`, `JUDGE_MAX_TOKENS`, `OPIK_API_KEY`, `OPIK_WORKSPACE`, `OPIK_PROJECT_NAME`, `EMBEDDING_MODEL`, `MONTHLY_LLM_BUDGET_USD`, `CONVERSATION_BUDGET_USD`. Secrets typed as `SecretStr`.

## Seed data design
- **Specialties (5)** + **Doctors (5)** per global understanding.md table.
- **Schedules:** each doctor works a subset of Mon–Sat within 09:00–18:00 minus 13:00–14:00 (e.g., D4 pediatric mornings only on Sat; D1 surgery day Wed → no OPD Wed).
- **Holidays (2026–2027, fictional-but-plausible IST holidays):** e.g., Republic Day 26 Jan, Independence Day 15 Aug, Gandhi Jayanti 2 Oct, Diwali, Christmas.
- **Leaves:** 2–3 leave blocks across doctors in the next 8 weeks (for "doctor on leave" tests).
- **Patients (~30):** Faker `en_IN` with a **fixed seed**; ~5 minors linked to guardian users; 3 with `no_show_count ≥ 3` (flagged); 1 elderly patient managed by an adult child. Clearly fake phone numbers (`+91 90000 0xxxx`) and `@example.com` emails.
- **Staff:** 1 admin, 2 front_desk, 5 doctor users. Dev passwords from env (`SEED_DEFAULT_PASSWORD`), never hard-coded secrets in repo beyond `.env.example` placeholder.
- **KB stubs (markdown with YAML front-matter):** 5 doctor bios, ~8 procedure FAQs (LASIK, cataract, glaucoma screening, dilated exam, diabetic eye screening, squint evaluation, dry eye, ptosis), `price_list_2025.md` (old, superseded) + `price_list_2026.md` (current, different LASIK price → conflict test), insurance list, prep instructions, cancellation policy, clinic info (hours/location/parking). Every doc carries a "clinic-approved, educational only" footer.

## Corner cases addressed in Phase 0
- Slot generation skips Sundays, holidays, leaves, lunch, and partial overlaps with leave ranges.
- Slot generation is idempotent.
- Partial unique index rejects two active holds on one slot (DB-level test).
- State machine rejects illegal transitions & wrong-role transitions.
- Policy: change at exactly 24h boundary → treated as free (≥24h); 23h59m → approval required.
- Minor age computed in clinic-local date (birthday-at-midnight edge case).
- Audit log UPDATE/DELETE blocked by trigger.

## Risks (phase-specific)
- SQLite partial index + trigger behavior through SQLAlchemy/Alembic batch mode → write a direct DB test.
- `zoneinfo` on Windows without `tzdata` → include `tzdata` dep.
- Docker not installed → Dockerfile/Compose not runnable locally; validate with `docker compose config` once available. Recorded in RETRO.

## Open questions (defaults assumed)
- Doctor names/schedules above OK? (assumed yes)

## Decisions Log
- **2026-11-10** — UUID string PKs everywhere.
- **2026-11-10** — `users` auth table + `patients`/`staff_users` profile tables; `patient_guardians` link table.
- **2026-11-10** — `appointment_slots` join with partial unique index `WHERE active` for double-booking prevention.
- **2026-11-10** — Added `rejected` status; ownership/time rules live in policy layer, not state table.
- **2026-11-10** — Only Phase-0 deps installed; heavy AI deps dry-run probed for Python 3.13 compatibility.
- **2026-11-10** — Append-only audit log enforced by SQLite triggers.
- **2026-11-10** — Compatibility probe (uv pip compile, py3.13, win): chromadb 1.5.9, sentence-transformers 6.2.0, torch 2.15.0, opik 2.2.143, langgraph 1.2.20, langchain-groq 1.1.3, groq 0.37.1, mcp 2.3.0 all resolve → **stay on Python 3.13**.
- **2026-11-10** — Deviation: `appointments` also stores denormalised `start_utc`/`end_utc` (fast listing/sorting); `appointment_slots` remains the source of truth for slot holds. `knowledge_docs` gained a `meta` JSON column for extra front-matter (doctor_code, procedure, currency).
- **2026-11-10** — Deviation: logging module named `netracare/log.py` (not `logging.py`) to avoid confusion with the stdlib.
- **2026-11-10** — Deviation: seed split into `seed/ids.py`, `patients.py`, `kb.py`, `runner.py`, `__main__.py`; YAML data in repo-root `seed/data/`. `--reset` = alembic downgrade base + upgrade head (needed because audit rows can't be deleted).
- **2026-11-10** — Postgres audit trigger also added to migration 0001 (not just SQLite).
- **2026-11-11** — Fixed phone-redaction false positive on ISO dates (≥10 digits, date shapes excluded).
- **2026-11-11** — Dev dep `httpx` → `httpx2` (Starlette TestClient deprecation). Removed `.tuples()` (SQLAlchemy 2.1 deprecation).
- **2026-11-11** — Seed tests use a module-scoped seeded DB (suite 193s → ~30s).
- **2026-11-11** — Clean-install check done in a scratch venv outside the repo instead of `.venv-check/`.
- **2026-11-18** — Git identity (repo-local): `divyaruchi24-ux <divya.ruchi24@gmail.com>`. Branch renamed `master` → `main`. Added `.gitattributes` (LF everywhere, CRLF for `.ps1`).
- **2026-11-18** — Public GitHub repo created: https://github.com/divyaruchi24-ux/Netracare-eyeclinic-agent (name uses hyphens; GitHub disallows spaces).
- **2026-11-18** — Fixed over-broad `data/` gitignore that excluded `seed/data/`; CI (incl. Docker build) green afterwards. Local `docker compose up` still unverified.
