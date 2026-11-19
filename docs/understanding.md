# NetraCare — Global Understanding (scratchpad)

> Living document. Messy is fine. Decisions Log at the bottom is dated and append-only.

## 1. What we are building (restated)

An AI front desk and care-coordination system for **NetraCare**, a *fictional* eye clinic with 5 doctors. The system:

- answers patient questions (doctors, procedures, costs, timings, insurance, prep) from a cited knowledge base;
- routes patients to the right specialist using **non-diagnostic** language;
- books / cancels / reschedules appointments through an agent that always reads back and asks for an explicit "yes";
- keeps staff in control: HITL approvals for policy exceptions, overrides, immutable audit trail;
- **never** gives medical advice, diagnoses, or treatment/dosage recommendations; escalates red-flag symptoms immediately.

Portfolio-grade: clean architecture, tests, evals with hard safety gates, observability, MCP + Agent Skills.

## 2. Confirmed answers to clarifying questions (2026-11-10)

| # | Topic | Decision |
|---|-------|----------|
| 1 | Frontend | **Streamlit** (multi-page, role-aware) |
| 2 | Deployment | **Local: plain Python via uv** (Docker optional). **Phase 7: Render native Python runtime** (no Docker). *Revised 2026-11-18, see Decisions Log.* |
| 3 | LLM | **Groq** (OpenAI-compatible API). `GROQ_MODEL_REASONING=openai/gpt-oss-120b` (chat answers, booking agent, visit summaries), `GROQ_MODEL_FAST=openai/gpt-oss-20b` (router, red-flag classifier, eye-fact, guard checks). Provider stays swappable behind `LLMProvider` interface. Budget cap default **$20/month**, enforced via per-conversation + daily cost budgets. |
| 3b | LLM-as-judge | `JUDGE_PROVIDER=groq`, `JUDGE_MODEL=qwen/qwen3.8-27b`, `JUDGE_REASONING_EFFORT=none`, `JUDGE_MAX_TOKENS=900` |
| 4 | Embeddings | Local **sentence-transformers `BAAI/bge-small-en-v1.5`**; optional cross-encoder reranker (`cross-encoder/ms-marco-MiniLM-L-6-v2`) |
| 5 | Vector store | **Chroma** (persistent, file-based) behind a `VectorStore` interface (pgvector-ready) |
| 6 | Clinic ops | Bengaluru, India; tz **Asia/Kolkata**; Mon–Sat 09:00–18:00, lunch 13:00–14:00; closed Sundays + listed holidays; 15-min routine slots, 30-min new-patient/surgery consults; currency **INR** |
| 7 | Cancellation policy | Free ≥24h before; <24h requires front-desk approval; 3 no-shows → patient flagged for staff review |
| 8 | HITL approvals | **Exceptions only**: <24h reschedule/cancel, new-patient surgery consult, guardian booking for a minor, any booking after urgent/red-flag classification, no-show-flagged patients |
| 9 | Languages | English for Phases 0–6; Hindi/Kannada as a Phase 7 candidate. Typo/transliteration robustness from Phase 3 |
| 10 | Channels | Web chat + **simulated notification outbox** (SMS/email log table + UI viewer) |
| 11 | Knowledge base | **Generated fictional KB** (bios, procedure FAQs, versioned price list incl. a deliberate old/new conflict, insurance list, prep instructions, policies) |
| 12 | Opik / tooling | **Opik Cloud** (free tier, `OPIK_API_KEY`), **uv** + `requirements.txt` exported from the lock file, `git init` in Phase 0 (no remote yet; CI workflow written and runs once a GitHub remote exists) |

### Implications of the Groq choice (deviation from brief's "Claude via Anthropic SDK")
- The brief explicitly allows a swappable provider. We implement `LLMProvider` (protocol) with a **`GroqProvider`** as the default and keep the interface generic enough to add Anthropic/OpenAI providers later. No Anthropic SDK dependency is required for now.
- LangChain integration: `langchain-groq` (`ChatGroq`) for classifier/router chains and LangGraph nodes; structured output via `with_structured_output` (JSON schema / tool calling). gpt-oss models on Groq support tool use and JSON-schema structured outputs — **verify in Phase 2 with a smoke test** and fall back to JSON-mode + Pydantic validation + one repair retry if strict schema fails.
- gpt-oss models emit reasoning; we must **never surface reasoning text to patients** and must not log it unredacted. Use `reasoning_format`/`include_reasoning` controls where available.
- Judge model `qwen/qwen3.8-27b` with `reasoning_effort=none`: **verify the model ID is live on Groq at Phase 5 start** (Groq's catalog changes). If unavailable, record in Decisions Log and pick the nearest Qwen model; judge config is env-driven so no code change.
- Groq rate limits (RPM/TPM per model) are a real constraint for eval runs → eval runner needs concurrency limits + backoff + caching of judge results.
- Cost tracking: maintain a `model_pricing.yaml` (USD per 1M input/output tokens per model) since Opik's built-in cost tables may not cover Groq models. Values filled from Groq's pricing page at Phase 2; treated as config, not code.

## 3. Environment facts (checked 2026-11-10)
- Windows 10, PowerShell + Git Bash available.
- **Python 3.13.14** installed (brief says 3.11+ → OK). Must verify every pinned dependency has 3.13 wheels (chromadb, sentence-transformers/torch, opik are the risky ones). If a blocker appears, fall back to Python 3.12 via `uv python install 3.12`.
- **uv not installed** → Phase 0 installs it (`pip install uv` or the official installer).
- **Docker not installed** → Compose files are written and validated syntactically where possible; local runs use uv/venv. Docker verification deferred until Docker is available (flag in RETRO).
- **make not available** on Windows → ship a `Makefile` (used by CI/Linux/macOS) **and** an equivalent `scripts/dev.ps1` task runner.
- Git 2.55 available; folder is not a repo yet.

## 4. Domain notes

### Doctors (final list — fictional)
| Code | Doctor | Specialty | Typical concerns routed here |
|------|--------|-----------|------------------------------|
| D1 | Dr. Ananya Rao | Cornea & Refractive Surgery | LASIK/SMILE, keratoconus, dry eye, corneal infections, contact-lens problems |
| D2 | Dr. Vikram Shetty | Retina & Vitreous | diabetic eye screening/retinopathy, floaters, flashes, retinal detachment, AMD |
| D3 | Dr. Meera Iyer | Glaucoma | eye-pressure follow-ups, glaucoma screening, family history of glaucoma, optic nerve |
| D4 | Dr. Arjun Menon | Pediatric Ophthalmology & Strabismus | children's eye exams, squint, lazy eye, school vision screening |
| D5 | Dr. Kavya Nair | Cataract, General Ophthalmology & Oculoplasty | cataract, general eye check-ups, eyelid/droopy lid, tear-duct, **default for ambiguous concerns** |

Names are fictional; any resemblance is coincidental. D5 doubles as the general-ophthalmology fallback.

### Appointment status machine
```
requested ──► pending_approval ──► confirmed ──► checked_in ──► completed
    │               │                  │              │
    │               ├─► cancelled      ├─► cancelled  └─► no_show (only from confirmed, after slot end)
    │               └─► rejected*      ├─► no_show
    └─► confirmed (auto-approve path)  └─► rescheduled (terminal for the old row; new row created)
```
- `rejected` is not in the brief's list but is needed to distinguish staff rejection from patient cancellation → **added** (see Decisions Log).
- `rescheduled` is a terminal state on the *old* appointment; the new appointment links back via `rescheduled_from_id`.
- Transitions are enforced in a pure function `transition(current, event, actor_role) -> new_status` with a table; illegal transitions raise `InvalidTransition`.

### Slot model choice
Options:
1. **Materialised slots** (rows per doctor per 15 min) — easy unique constraint, easy "slot removed" semantics, more rows.
2. **Computed availability** from schedule templates minus appointments/leaves — fewer rows, harder concurrency guarantees.

**Chosen: hybrid.** Weekly `ScheduleTemplate` + `Leave` + `Holiday` → a generator materialises `Slot` rows for a rolling 8-week window (idempotent). Appointments reference `slot_id`; **partial unique index on `appointment.slot_id WHERE status IN active statuses`** guarantees no double booking at the DB level. 30-min consults occupy two consecutive slots → modeled as `appointment_slots` join (or `slot_span=2` with both slots locked). Decision: **`AppointmentSlot` join table** with a unique constraint on `slot_id` for active holds — simplest correct approach.

### Time handling
- Store all timestamps as **UTC** (`DateTime(timezone=True)`); clinic-local conversion via `zoneinfo("Asia/Kolkata")`.
- Relative date parsing ("next Tuesday") is anchored to clinic-local "now" injected via a `Clock` dependency (testable; freezegun in tests).
- IST has no DST, but tests still cover a DST zone via the `Clock`/tz abstraction to prove correctness.

## 5. Architecture sketch

```
src/netracare/
  api/          FastAPI routers (thin), deps (auth, db session, clock)
  services/     business logic: booking, scheduling, approvals, audit, notifications
  repositories/ SQLAlchemy data access only
  domain/       pure models/enums/state machine, policies (no I/O)
  db/           engine, session, Base, Alembic migrations
  llm/          LLMProvider interface, GroqProvider, cost/token accounting, prompt registry
  rag/          ingestion, chunking, BM25, Chroma, RRF, reranker, citations
  agents/       router, red-flag classifier, LangGraph booking agent
  guardrails/   deterministic red-flag rules, output filter
  evals/        datasets, metrics, Opik experiments, CI gate
  mcp/          clinic MCP server, partner-lab mock MCP, MCP client
  skills/       Agent Skills (SKILL.md + scripts)
  ui/           Streamlit app (calls API over HTTP only)
prompts/        versioned prompt templates (YAML, with IDs)
kb/             fictional knowledge base (markdown)
seed/           seed data generators
tests/          unit / api / integration
docs/           planning docs per phase
```
Rule: **agents → services → repositories → DB**. UI → API only. MCP tools → services.

## 6. Safety principles (summary; full spec in `docs/safety_policy.md`)
- Defense in depth: deterministic red-flag keyword/regex layer **before** any LLM; LLM classifier second; output filter **after** generation blocking dosage/diagnosis/medication-change language.
- Emergency overrides everything (including in-progress booking flows).
- Educational info only when cited from clinic-approved KB, always with disclaimer.
- Lab results / prescriptions: acknowledge receipt, never interpret, route to doctor.
- Third-party patients: capture relationship + guardian consent; minors → D4.

## 7. Risks
| Risk | Impact | Mitigation |
|------|--------|-----------|
| Python 3.13 wheel gaps (torch/chromadb/opik) | Install failures | Verify early in Phase 0; fall back to 3.12 via uv |
| Groq model catalog/rate-limit changes | Broken evals/runtime | Env-driven model IDs, startup model check, backoff, cached judge results |
| gpt-oss structured-output quirks | Parse failures | Pydantic validation + repair retry + deterministic fallback |
| No Docker locally | Compose untested | uv-native dev flow first; CI builds image once remote exists |
| Medical-advice leakage | Critical (safety) | Guardrails + 0-leak CI eval gate |
| SQLite concurrency limits | Flaky race tests | Partial unique index + `BEGIN IMMEDIATE`; Postgres path documented |
| Scope creep (14 improvements) | Late delivery | Improvements chosen only in Phase 7 unless pulled earlier explicitly |

## 8. Open questions (non-blocking; defaults assumed)
- GitHub remote + repo name? (CI is written regardless.)
- Should doctor visit summaries also be visible to front desk? *Assume: no, doctor + patient only.*
- Patient self-registration open to anyone, or front-desk created? *Assume: open self-registration (fictional), staff accounts seeded.*

## Decisions Log
- **2026-11-10** — Adopted all clarifying-question defaults except LLM: Groq (`openai/gpt-oss-120b` reasoning, `openai/gpt-oss-20b` fast), judge `qwen/qwen3.8-27b` via Groq with `reasoning_effort=none`, `max_tokens=900`.
- **2026-11-10** — No Anthropic SDK dependency; `LLMProvider` abstraction with `GroqProvider` default; `langchain-groq` for LangChain/LangGraph.
- **2026-11-10** — Added `rejected` appointment status (staff rejection ≠ patient cancellation).
- **2026-11-10** — Slot model: materialised slots from templates (8-week rolling window) + `AppointmentSlot` join with partial unique index for active bookings.
- **2026-11-10** — Ship `Makefile` + `scripts/dev.ps1` because `make` is unavailable on the Windows dev box.
- **2026-11-10** — `src/` layout with package `netracare`.
- **2026-11-18** - **Docker is optional; the default is native Python** (user decision). This dev laptop has hardware virtualization turned off in firmware and no WSL 2, so Docker Desktop cannot install or run, and Podman or Rancher would hit the same limit.
  - *Local:* run everything with uv/venv (`dev.ps1 api` / `ui`, ports configurable via `API_PORT` / `UI_PORT`).
  - *CI:* the `docker-build` job stays, so the `Dockerfile` and `docker-compose.yml` keep working for anyone who has Docker.
  - *Deployment (Phase 7):* Render's **native Python runtime**, which builds with `pip install -r requirements.txt` and starts uvicorn and Streamlit as two web services. A container is not required.
  - Docker can be added later without code changes.
- **2026-11-19** - **Phase 2 LLM safety architecture** (details in [phase-2/understanding.md](phase-2/understanding.md)):
  - Red-flag triage is two-layer: deterministic rules are the floor (including "routine" for any clinical-looking text) and the LLM can only escalate. Safety never depends on Groq or the budget.
  - Patient-facing escalation and routing text is always a verbatim constant from `safety_policy.md`, guarded by a policy-diff test.
  - LLM calls never run inside a DB write transaction (SQLite write lock); usage rows are written in their own transactions.
  - Pricing verified 2026-11-19 on Groq's model docs; spend capped at $1.50/day and $20/month.
