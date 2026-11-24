# Phase 3 — Retrospective (patient chat with RAG)

**Dates:** 2026-11-22 (planning) – 2026-11-24 (execution) · **Commit:** `phase-3: patient chat with hybrid RAG, cited answers, intent router`

## What shipped
- **Ingestion** (`netracare/rag/`):
  - Markdown, plus PDF with a sidecar `<file>.meta.yaml`.
  - Heading-aware chunking: tables are never split, footers are stripped, and each chunk keeps its heading path.
  - A `kb_chunks` table is the citation source of truth.
  - Incremental and idempotent: chunk checksums, plus a fingerprint that detects a stale vector store.
  - A prompt-injection scan refuses poisoned chunks.
  - `python -m netracare.rag.ingest`, `dev.ps1 ingest`; `reseed` also ingests.
- **Hybrid retrieval:**
  - BM25 (typo map, synonym folding, Hinglish price words) plus `BAAI/bge-small-en-v1.5` through **fastembed** (ONNX, no torch) in **Chroma**, fused with RRF.
  - Fuzzy doctor names ("Dr Shetti" → Dr. Vikram Shetty), soft boosts (doctor, price list), and an optional reranker (off by default; see below).
  - A calibrated confidence threshold (0.605).
  - **Superseded documents are excluded at retrieval time**, so the latest effective date always wins.
- **Cited answers** (`chat_answer@v1`, gpt-oss-120b):
  - Code-level post-checks run before anything reaches a patient: valid `[n]`; every number present in a cited chunk; no medicine, dose, diagnosis or treatment wording beyond the sources; no prompt leakage.
  - One repair round-trip, then an **extractive fallback** (the KB text itself, cited).
  - §10 disclaimers are added by code, including the price line with the cited list's effective date.
- **Two-layer router:**
  - *Layer 1 (rules, before any LLM):* emergencies and urgent cases (verbatim scripts, deduplicated alerts), medical and lab-report questions (§5.3), human requests, empty/emoji/spam, and small talk.
  - *Layer 2:* `intent_router@v1` (gpt-oss-20b) with follow-up rewriting, and a keyword fallback.
  - Symptom-bearing messages also get the full Phase 2 triage, floored at urgent when the router says "emergency".
- **Specialist routing:**
  - A reviewed concern lexicon (`config/specialty_routing.yaml`) and §6 templates only; doctor names come from the DB.
  - Children → Dr. Arjun Menon. Ambiguity → one clarifying question, then Dr. Kavya Nair.
  - Booking is handed to Phase 4 as a "Book with Dr. X" action that pre-fills the booking page.
- **ChatService:**
  - Sessions: signed-in patients own theirs; anonymous visitors hold a token, of which only the hash is stored; anyone else gets 404.
  - Limits: length, turns, spam (which ends the session and flags staff), frustration (handoff), and a per-session budget (which switches to no-LLM mode).
  - Retention: a 90-day purge CLI.
  - Tracing: one Opik `chat.turn` trace with retrieval and LLM spans, and no patient text.
- **API:**
  - `/chat/sessions` (create, get, messages, handoff), `/kb/chunks/{id}`, `/kb/docs`.
  - `/chat/handoffs` (front desk: list, claim, close, conversation).
  - `/admin/kb`, `/admin/kb/reindex`, `/admin/kb/search` (retrieval debugger).
  - The permission matrix covers all 15 new routes.
- **UI:**
  - **Ask NetraCare**, for signed-in and anonymous users: sources expander, red/amber safety boxes, booking action, "Talk to a person", and the always-visible §10 footer.
  - **Chat handoffs** (front desk).
  - **Knowledge base** (admin: status, superseded badges, refused chunks, re-index, search debugger).
- **Evals:**
  - `rag_retrieval` and `router_rules` are new offline **CI gates**.
  - `router_llm` and `rag_answer_live` are live suites logged to Opik.
  - `scripts/rag_sweep.py` handles k / rerank / threshold tuning.
  - CI caches the model files.
- **Phase 2 carry-overs fixed:**
  - Retried emergency bookings no longer raise a second alert (dedup key).
  - rf-026 routine-vs-non-clinical chatter is handled by the chat router.

## Verification
| Check | Result |
|---|---|
| Tests | **1,038 passed**, 2 skipped (opt-in live modules) |
| Coverage | 97% overall; `rag/` 98%, `services/` 97%, `llm/` 99%, `agents/` 99% |
| ruff / ruff format / mypy strict | clean (161 source files) |
| Clean venv `pip install -r requirements.txt -r requirements-dev.txt` | OK, `pip check` clean; chromadb 1.5.9, fastembed 0.9.1 import; **no torch** in the lock |
| `rag_retrieval` (offline gate, real models) | recall@1 0.913, **recall@3 1.0, recall@5 1.0**, MRR 0.949, **no superseded leak 1.0**, out-of-KB refusal 0.90, in-KB answerable 0.978 |
| `router_rules` (offline gate) | exact intent **1.0**, emergency recall **1.0** (55 cases) |
| `safety_redflags_rules` | emergency recall 1.0, urgent recall 1.0, no under-triage 1.0 (35 cases, incl. new E7 boundary cases) |
| `router_llm` (live, Opik `router_llm-34bb2f8-20261124T172451`) | intent accuracy **0.938** (target ≥ 0.85), emergency recall 1.0, medical-question recall 1.0, follow-up rewrite 1.0 |
| `rag_answer_live` (live, Opik `rag_answer_live-34bb2f8-20261124T171214`) | **no_advice 1.0, numbers_grounded 1.0**, citation_valid 1.0, refused_when_expected 1.0 (incl. prompt injection), expected_text 1.0 (current prices only), answered_with_source 0.952 |
| Groq smoke test | PASS, including the new chat turn: router → pricing, retrieval confidence 0.737, cited answer "52,000 [1]" with no superseded price |
| Live demo (port 8001, real Groq) | typo price question cited from the 2026 list with the dated price disclaimer; follow-up answered; out-of-KB → front-desk number; eye-drops question → §5.3 + Cornea; hidden bleach emergency → verbatim script + rinse line, repeat reused the same alert; child check-up → Dr. Arjun Menon pre-fill; "talk to a person" → handoff claimed and closed by the front desk; admin KB: 77 chunks in sync, "Dr Shetti" → Dr. Shetty, no-op re-index |
| Model smoke (this laptop) | first download ~40 s per model (152 MB); cached load ~2.2–2.6 s; query embedding ~30 ms; full KB embedding 50–70 s |

## What went well
- Excluding superseded documents at retrieval time made "latest price wins" a deterministic property (`no_superseded_leak` = 1.0), not something to hope the model gets right.
- The tuning sweep gave a clear, numbers-backed answer on rerank, which the user had made conditional: recall@3/5 is 1.0 either way, and it costs 0.3–2 s per query on CPU, so it stays off.
- Checking answers relative to their cited sources (numbers, medicines, doses) let the filter stay strict without blocking legitimate KB wording such as "dilating drops" or "bring a driver".
- The live evals found three real issues that unit tests could not: missing inline markers, an over-broad medical detector, and the rewrite-vs-threshold edge. Each was fixed with a regression test or eval case.
- The safety gates caught a regex corruption immediately (see below).

## What was hard / surprises
- **gpt-oss citation behaviour.** The model fills the `citations` field but rarely writes `[n]` inline, so every first-run answer fell back to extractive mode. It is fixed in code (`with_markers`), so the post-checks still apply.
- **Shell quoting, again.** A heredoc turned `\b` into backspace characters in `redflag_rules.py`. `safety_redflags_rules` and `router_rules` failed at once; the file was repaired by replacing `chr(8)` and verified byte by byte.
- **Narrow confidence margins** on a 23-document KB: in-KB and out-of-KB cosine scores overlap within about 0.01 around the threshold. The LLM's `answerable=false` is the second gate, and it refused 100% of the live out-of-KB and injection cases. The baseline floors use the plan targets so tiny numeric differences across platforms can't flip CI.
- **CPU speed:** embedding the KB takes about a minute on this laptop. The admin re-index has a long timeout and ingestion embeds before opening its write transaction.
- **Log redaction bug** (from Phase 2): the phone pattern masked digit runs inside UUIDs, garbling request IDs in logs. It is fixed, with a regression test.
- A Phase 2 E7 rule over-triaged any "droopy eyelid" as an emergency. It now needs "sudden", a nerve sign or a headache, matching policy 4.1, with eval cases on both sides.

## Carry-overs
- Router prompt tuning (Phase 5): privacy → off-topic, "Dr Shetti timings" → faq, "what can you do?" → off-topic, and "Do I need to fast before cataract surgery?" over-refused (safe).
- Recalibrate the retrieval threshold on ≥ 50 RAG questions (Phase 5); consider a lexical-semantic agreement signal.
- Multilingual (Hindi/Kannada) answers: Hinglish retrieval works through BM25 synonyms, but answers are in English (Phase 7 candidate).
- Facial droop with slurred speech and no eye mention gives no flag (stroke signs, outside policy 4.1); add it to Phase 5's red-team set.
- The booking agent replaces the "Book with Dr. X" handoff in Phase 4.
- Phase 1/2 carry-overs (Postgres, a shared rate-limit store, Qwen judge availability) remain.

## Metrics snapshot
- Prompts: `chat_answer@v1`, `intent_router@v1` (new); `red_flag_classifier@v1`, `eye_fact@v1`, `visit_summary@v1`, `json_repair@v1`.
- KB: 23 documents → 77 chunks (largest 245 tokens), fingerprint `95890fe51f3d`.
- Live Groq spend for Phase 3 (smoke tests, four live eval runs, demo): well under $0.10.
