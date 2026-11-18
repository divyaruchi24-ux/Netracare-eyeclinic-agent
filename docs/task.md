# NetraCare — Global Task Checklist

Each phase links to its detailed `task.md`. A phase box is ticked only when all its exit criteria (see [plan.md](plan.md)) are met and the `phase-N:` commit exists.

- [x] **Phase 0 — Foundations & domain modeling** → [phase-0/task.md](phase-0/task.md)
  - [x] Planning docs approved
  - [x] Tooling & repo scaffold
  - [x] Domain model + migrations
  - [x] Seed data + fictional KB stubs
  - [x] Safety policy
  - [x] Tests (107 passing, 97% cov), [RETRO](phase-0/RETRO.md), commit `phase-0: ...`
- [x] **Phase 1 — Clinic website** → [phase-1/task.md](phase-1/task.md)
  - [x] Planning docs written
  - [x] Planning docs approved
  - [x] Foundations, migration 0002, auth
  - [x] Access policy, directory, scheduling, availability
  - [x] Booking core + race/idempotency tests, approvals
  - [x] Front desk, doctor, admin, permission matrix
  - [x] Streamlit dashboards
  - [x] Opik tracing + eval harness + CI eval gate (foundation; full suite in Phase 5)
  - [x] Tests, RETRO, commit `phase-1: ...`
- [ ] **Phase 2 — First LLM features** → phase-2/task.md
- [ ] **Phase 3 — Patient chat with RAG** → phase-3/task.md
- [ ] **Phase 4 — Booking agent (LangGraph)** → phase-4/task.md
- [ ] **Phase 5 — Evals & LLMOps (Opik)** → phase-5/task.md
- [ ] **Phase 6 — MCP & Agent Skills** → phase-6/task.md
- [ ] **Phase 7 — Hardening & polish** → phase-7/task.md

## Corner-case tracker (assigned phase → eval in Phase 5)
| Corner case | Implemented in | Eval'd in |
|---|---|---|
| Emergency hidden in casual text overrides booking | 2 (classifier), 3 (router), 4 (agent) | 5 |
| Sudden vision loss / curtain / flashes+floaters / chemical / trauma / pain+nausea+halos | 0 (policy), 2 | 5 |
| Drops / pressure value / stop medication → no advice | 0 (policy), 2–3 | 5 |
| Lab report / prescription interpretation request | 3, 6 | 5–6 |
| Third-party patient, guardian consent, pediatric routing | 1 (data model), 3–4 | 5 |
| Ambiguous symptoms → clarify / general ophthalmology | 3 | 5 |
| Race on same slot, double-submit, duplicate booking | 0 (constraints), 1 | 1 (tests) |
| Doctor leave / removed slot after proposal, holidays, past dates, out-of-hours | 0–1, 4 | 4–5 |
| Relative dates & tz/DST | 0 (Clock), 4 | 4–5 |
| Policy-window reschedule/cancel, no-show history, cross-patient authZ | 1 | 1, 5 |
| Mind change / abandon / resume | 4 | 5 |
| Tool failure mid-transaction | 1, 4 | 4 |
| Staff approval timeout | 4 | 4 |
| Not-in-KB → honest handoff | 3 | 5 |
| Stale/conflicting docs → latest effective date | 0 (KB), 3 | 5 |
| Typos / misspelled doctors / transliteration | 3 | 5 |
| Long / empty / emoji-only / spam inputs | 1 (validation), 3 | 5 |
| Prompt injection (chat, KB, lab data) | 2–3, 6 | 5 |
| Other patients' data / PII in logs & traces | 1, 2 | 5, 7 |
| Rate limiting / abuse / cost runaway | 1, 2, 4 | 7 |
| LLM outage → graceful degradation | 2–3 | 5, 7 |
| Judge–human disagreement / drift | 5 | 5 |
