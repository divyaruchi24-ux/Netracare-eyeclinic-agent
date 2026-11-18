# Phase 0 — Retrospective

**Dates:** 2026-11-10 → 2026-11-18 · **Status:** complete (Docker run verification deferred; see below)

## Delivered
- uv project (`pyproject.toml`, `uv.lock`), pinned `requirements.txt` + `requirements-dev.txt` exported from the lock file. A clean venv install from those files passes the full suite.
- Tooling: ruff (lint + format), mypy `--strict` (pydantic plugin), pytest + coverage, pre-commit, `Makefile` + `scripts/dev.ps1`, GitHub Actions CI with three jobs: uv lint/type/test, plain-pip requirements install, and Docker build.
- `Settings` covering the Groq models, judge, Opik, retrieval and budgets; `.env.example`; structlog JSON logging with request-id and PII redaction; FastAPI `/health`.
- Pure domain layer: enums, injectable `Clock`, appointment state machine (adds a `rejected` state), approval/24h/no-show/minor policies, slot generation that is DST-safe.
- Persistence: 15 tables and Alembic `0001`. Constraints:
  - a partial unique index blocks double-booking (one active hold per slot)
  - an idempotency-key unique constraint
  - optimistic `version` column
  - **append-only audit log enforced by DB triggers** (SQLite + Postgres)
- Idempotent seed with deterministic UUIDs:
  - 5 specialties, 5 doctors, 24 schedule blocks, 9 holidays, 3 leaves, ~4.8k slots
  - 30 patients: 5 minors and 1 elderly patient with guardians, 3 no-show-flagged
  - 38 users and 8 staff
  - 23 KB docs, including a v1/v2 price-list conflict
- `docs/safety_policy.md` v1.0.

## Verification
| Check | Result |
|---|---|
| `ruff check .` / `ruff format --check .` | clean |
| `mypy` (strict, 33 files) | clean |
| `pytest` | **107 passed**, total coverage **97%**, `domain/` ≈100% |
| Models vs migration (autogenerate diff) | empty |
| `alembic upgrade head` → `downgrade base` | OK (test) |
| `python -m netracare.seed --summary`, run twice | second run: "nothing new" |
| `uvicorn` + `GET /health` | `{"status":"ok","version":"0.1.0"}` |
| Clean venv `pip install -r requirements.txt -r requirements-dev.txt` + pytest | 107 passed |
| Docker build / compose up | **not run — Docker is not installed locally.** CI `docker-build` job will cover it once a GitHub remote exists. |

## What worked
- Checking dependency resolution early confirmed all the heavy Phase 2–6 deps (torch, chromadb, opik, langgraph, langchain-groq, mcp) resolve on Python 3.13, so no downgrade is needed.
- The models-vs-migration diff test will catch schema drift automatically from now on.
- Deterministic `uuid5` seed IDs made idempotency simple and testable.

## What broke / surprises
- **Redaction bug** (caught by a new test): the phone regex masked ISO dates (`2026-11-10` → `[PHONE]`). It now requires ≥10 digits and excludes date shapes. Phase 2 will extend redaction further, so this regression test matters.
- SQLAlchemy 2.1 deprecates `Result.tuples()`; replaced with plain row unpacking.
- Starlette deprecates `httpx` for its TestClient, so the dev dep was switched to `httpx2`.
- Seed tests initially took about 3 minutes with coverage on (each test re-seeded 8 weeks of data). A module-scoped seeded DB brought the full suite to about 30s.
- `uv` isn't on PATH after `pip install --user`, so scripts call `python -m uv`. `UV_LINK_MODE=copy` is set because the cache and the project are on different drives.
- `make` is unavailable on Windows; `scripts/dev.ps1` mirrors the Makefile.

## Carry-overs / next
- Install Docker Desktop (optional, needed by Phase 7) and verify `docker compose up` + `--profile seed`.
- Add a GitHub remote so CI runs.
- Seeded leaves are relative to the seed date, so re-seeding on a later day adds new upcoming leaves. That's intended for demos; documented here.
- `Settings` has a dev-only default `SEED_DEFAULT_PASSWORD`; Phase 1 must refuse to start with default secrets when `ENVIRONMENT != dev`.
- Phase 1: auth/JWT/RBAC, booking services on top of the state machine + policies, the 4 Streamlit dashboards, and a concurrency test against the partial unique index.
