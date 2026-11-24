# Phase 3: Understanding (patient chat with RAG)

> Scratchpad. Messy is fine. The dated Decisions Log at the bottom is append-only.

## 1. What the brief asks for (restated)

1. **Ingestion.** Markdown or PDF knowledge base → heading-aware chunks → metadata (doc type, doctor, procedure, effective date, version) → a BM25 index plus a vector index.
2. **Hybrid retrieval.** BM25 plus semantic search, fused with Reciprocal Rank Fusion (RRF), metadata filters, an optional cross-encoder rerank, and a tuned `k`.
3. **Cited answers.** Inline citations like `[1]` that link to the source chunks. When retrieval confidence is low, refuse and redirect ("I don't have that information, let me connect you to the front desk").
4. **Router.** Intents: `faq_rag | doctor_lookup | pricing | booking (handoff to Phase 4) | medical_question (safe refusal + routing) | emergency | human_handoff | chit_chat/off_topic`.
5. **Specialist routing.** Concern → specialty → doctor, explained without diagnosis ("Concerns about floaters are typically seen by our Retina specialist").

Global exit criteria ([plan.md](../plan.md)):
- retrieval tests (recall@k on a seed set)
- low confidence → handoff
- citations resolve to chunks
- the latest effective date wins on conflicts

Corner cases this phase owns ([task.md tracker](../task.md)):
- not in KB → honest handoff
- stale or conflicting docs
- typos, misspelled doctor names, transliteration
- long, empty, emoji-only or spam input
- ambiguous symptoms → clarify, then General Ophthalmology
- lab report or prescription pasted for interpretation
- third-party or child questions → pediatric routing
- prompt injection in chat **and inside KB documents**
- emergency hidden in casual chat
- LLM outage → graceful degradation
- the Phase 2 carry-overs: rf-026 (routine vs non-clinical chatter) and duplicate alerts on retried emergencies

## 2. Current state (what we build on)

- **KB.** 23 markdown files under `kb/`, about 3,600 words, with YAML front matter. Required keys are `slug, title, doc_type, version, effective_date`; optional ones are `doctor_code, specialty, procedure, currency`. `seed/kb.py` already parses them into `knowledge_docs` rows and links `superseded_by_id`.
  - The deliberate conflict: `price-list` v1 (2025-04-01, LASIK ₹45,000) vs v2 (2026-04-01, LASIK ₹52,000).
- **Config** already has `embedding_model=BAAI/bge-small-en-v1.5`, `reranker_model=cross-encoder/ms-marco-MiniLM-L-6-v2` and `chroma_path=./data/chroma` (from Phase 0). No RAG code exists yet.
- **Safety building blocks from Phase 2**, reused as they are:
  - `redflag_rules.detect / level_for / looks_clinical / specialty_for / mentions_child / asks_medical_advice`
  - `TriageService.classify`, which runs rules + LLM and creates alerts
  - `scripts.py` (§5.1–5.4, §6 routing templates, §10 disclaimers)
  - `advice_filter` (advice and drug patterns, number checks)
  - `LLMClient` (budget, retries, structured output, cost, Opik spans), `PromptRegistry`, `redact_all`, `SlidingWindowLimiter`

## 3. Design options and choices

### 3.1 Embeddings runtime: sentence-transformers (torch) vs fastembed (ONNX)
| | sentence-transformers | **fastembed** |
|---|---|---|
| Models | bge-small, ms-marco cross-encoder | the same two models as ONNX exports (`BAAI/bge-small-en-v1.5`, `Xenova/ms-marco-MiniLM-L-6-v2`) |
| Install | pulls **torch 2.15** (~250 MB on Windows, more on Linux with CUDA unless the CPU index is pinned) | onnxruntime, which **chromadb already pulls in**; no torch |
| Cold start / RAM | slow import, ~600 MB RAM | fast, ~150 MB |
| Render (Phase 7) | risky on small instances | fits |

Both resolve on Python 3.13 (checked 2026-11-22: chromadb 1.5.9, fastembed 0.9.1, rank-bm25 0.2.2, onnxruntime 1.32.0).

**Proposal: fastembed, with the same model IDs.** This deviates from the Phase 0 wording "sentence-transformers", but not from the models. It sits behind an `Embedder` / `Reranker` protocol, so sentence-transformers can be swapped back later. The reranker setting value changes to `Xenova/ms-marco-MiniLM-L-6-v2`, fastembed's name for the same model.

### 3.2 Vector store
Chroma (decided in Phase 0) runs as a `PersistentClient` at `data/chroma`, with telemetry off, behind a `VectorStore` protocol. Unit tests use an `InMemoryVectorStore` (NumPy cosine). The collection name includes an **index fingerprint** made of the embedding model, the chunker version and the KB checksum set. A stale index is detected at startup and rebuilt rather than served silently.

### 3.3 Source of truth for chunks
A new `kb_chunks` table (migration 0004) holds:
- `id`, `doc_id`, `ordinal`, `heading_path`, `text`, `token_count`
- metadata copied from the document
- `is_current` (false when the document is superseded)

This is what makes citations "resolve to chunks": `GET /kb/chunks/{id}` returns the exact text the model saw. BM25 is built in memory from this table at startup; about 100 chunks takes milliseconds. Chroma stores only the ids, vectors and filter metadata.

### 3.4 Chunking (heading-aware)
- Split on `#`/`##`/`###`. Each chunk carries its **heading path** ("LASIK FAQ › What does it cost?").
- **Tables are never split.** The price list stays one chunk, or is split by row groups with the table header repeated.
- Target is about 250 tokens, hard max 400. Long sections split on paragraph boundaries with a one-sentence overlap.
- The text sent for embedding is `title › heading path \n\n body`. That makes short sections ("Who do I see?") retrievable.
- The front-matter footer disclaimer lines (`*Clinic-approved general information…*`) are stripped from chunk text; the UI shows the disclaimer anyway.
- **PDF.** `pypdf` extracts text page by page. Metadata comes from a sidecar `<file>.meta.yaml`, because PDFs have no front matter. Headings are detected heuristically: short lines in title case or all caps. The KB itself stays markdown; the PDF path is tested with a tiny generated PDF fixture.

### 3.5 Retrieval pipeline
```
query → normalise (lowercase, typo map, doctor-name fuzzy fix) → filters
      → BM25 top-20  ┐
      → vector top-20 ┴→ RRF (k=60) → [optional rerank top-10] → top-k (default 5)
```
- **Filters:** `is_current=True` by default. The latest effective date wins because superseded chunks are excluded at retrieval time, not left for the LLM to judge. The router can add `doc_type` (pricing → `price_list`), `doctor_code` and `specialty`.
- **Typos and transliteration:**
  - BM25 tokens go through a small typo map, extending `_TYPOS` from Phase 2 ("lasic", "catract", "glacoma").
  - A doctor-name matcher uses `difflib` (stdlib) against the 5 doctor names and surnames ("Dr Shetti" → Dr. Vikram Shetty).
  - Semantic search covers paraphrase.
  - Hinglish ("lasik ka price kitna hai") works partly: the English model still matches "lasik price". This is documented as a limit; real multilingual support is a Phase 7 candidate.
- **k is tuned** by a sweep over k ∈ {3, 5, 8} and rerank on/off on the retrieval eval set. The chosen values are recorded in the Decisions Log.

### 3.6 Retrieval confidence → refuse or answer
The options were the raw RRF score (not comparable across queries), the cosine of the top semantic hit, and the reranker score.

**Chosen:** `confidence = top-1 rerank score` when rerank is on, else `top-1 cosine`. The threshold (`rag_min_confidence`) is **calibrated** on the eval set's in-KB vs out-of-KB questions: pick the value that keeps in-KB recall ≥ 0.95 and refuses as many out-of-KB questions as possible. A second, independent gate is the generator's own `answerable=false`. Below the threshold, or when `answerable=false`, the reply is the §5.4 script verbatim plus a handoff offer. **No LLM call is made below the threshold**, which saves cost and removes any chance of hallucinating.

### 3.7 Answer generation
- Prompt `chat_answer@v1` on the **reasoning model** (gpt-oss-120b), with strict JSON output `{answer, citations: [int], answerable: bool}`. Chunks are numbered `[1]…[n]` inside data-only delimiters, and the prompt says that KB text is **data, never instructions** (§9).
- Deterministic **post-checks** before anything reaches the patient:
  1. every `[n]` in the answer exists among the given chunks, and at least one citation is present;
  2. **every number or price in the answer appears in a cited chunk**, reusing `advice_filter._numbers`. This is the "no hallucinated prices" check;
  3. the advice filter (dose, diagnosis, medicine names, "you should stop/start") reports nothing;
  4. there is no reference to other patients and no system-prompt leakage (a small pattern list).

  If any check fails, do one repair round-trip with the failure reasons. If it fails again, give the extractive fallback (below).
- **Disclaimers (§10)** are appended by code, not the model: the KB answer line always, and the price line with the cited list's effective date when a `price_list` chunk is cited.
- **Extractive fallback.** When the LLM is unavailable, over budget or fails the checks, reply with "Here's what our clinic information says:" followed by the top 1–2 chunks (trimmed) and their citations. This is graceful degradation that can't hallucinate, since it is the KB text itself.

### 3.8 Router (two layers again)
**Layer 1: deterministic, runs first on every message.**
1. Empty, emoji-only or punctuation → `chit_chat` with a fixed prompt to ask a question. Over 2,000 characters → 422. The same message 3 times in a session → a polite end and a staff flag (spam).
2. **Red-flag rules** (Phase 2): emergency or urgent → the `emergency` intent → `TriageService.classify`, which returns the verbatim script and creates an alert. **This overrides everything, including a hidden emergency inside a booking or FAQ message.** For urgent messages, the routing sentence is added after the script.
3. `asks_medical_advice`, or a lab-report/prescription pattern (§8: "my report says", "HbA1c", "IOP 24", "prescription", pasted value tables) → `medical_question` → §5.3 refusal with the §6 routing sentence. The topic is "test results" for reports.
4. An explicit request for a human ("talk to a person", "call me", "real human") → `human_handoff`.

**Layer 2: LLM router** (`intent_router@v1`, fast model). Output:
`{intent, confidence, standalone_query, specialty_hint?, doctor_mention?, concern_phrase?}`.
- `standalone_query` rewrites follow-ups ("and how much is it?" → "How much does LASIK cost?") from the last 3 turns, so query rewriting costs no extra call.
- Confidence below 0.6 → `faq_rag` if retrieval confidence passes, else `human_handoff` (§12).
- The LLM can **never downgrade** a layer-1 decision; layer 1 short-circuits before the LLM is called.
- If the LLM is down: keyword fallback (price/cost/fee → pricing; "dr"/doctor name → doctor_lookup; book/appointment/reschedule/cancel → booking; else faq_rag).

**Intent handlers:**
| Intent | Handler |
|---|---|
| `faq_rag` | retrieve → confidence gate → generate → post-checks |
| `pricing` | the same, with `doc_type=price_list` boosted (and procedure docs for "what's included"); price disclaimer |
| `doctor_lookup` | **DirectoryService** (DB, authoritative: name, specialty, OPD days) plus bio chunks as citations; fuzzy name match; never "who is best" (§3.7) |
| `booking` | Phase 4 handoff: the routing sentence (if there is a concern) + "You can book on the **Book appointment** page" + a `suggested_action` the UI turns into a button with the specialty and doctor prefilled |
| `medical_question` | §5.3 verbatim + §6 routing sentence |
| `emergency` | Phase 2 triage script + alert (deduplicated, §3.10) |
| `human_handoff` | create a `chat_handoff` with a redacted context summary; reply with the §5.4 wording or the front-desk number for anonymous users |
| `chit_chat` / `off_topic` | fixed friendly replies ("I'm NetraCare's AI assistant…", §9: never claims to be human) |

### 3.9 Specialist routing
- Rules first: `specialty_for(codes, text)` and `mentions_child` → PEDIATRIC (D4).
- A new **concern lexicon**, `config/specialty_routing.yaml`, maps non-red-flag concerns to specialties: dry eye, LASIK and contact lenses → CORNEA; floaters, diabetes and macula → RETINA; pressure and family history of glaucoma → GLAUCOMA; squint and lazy eye → PEDIATRIC; cataract, eyelid, droopy lid and tear duct → OCULOPLASTY_GENERAL. It is data, reviewed like policy.
- After that comes the LLM `specialty_hint` and `concern_phrase`, which pass through `safe_concern`.
- **Ambiguity:** two or more specialties match, or the rules and the LLM disagree, and no clarification has been asked yet → ask **one** clarifying question ("Is this mainly about … or …?") and store `pending_clarification` on the session. Still ambiguous → GENERAL (Dr. Kavya Nair) with the §6 "more than one area" sentence. Never guess silently.
- The doctor name comes from DirectoryService (the active doctor for the specialty), never from the LLM.

### 3.10 Conversation state, persistence, ownership
- Tables (migration 0004):
  - `chat_sessions`: id, `user_id?`, `patient_id?`, `anon_token_hash?`, `created_at`, `last_active_at`, `turns`, `cost_usd`, `status` (active, ended, handoff), `pending_clarification` JSON
  - `chat_messages`: id, session_id, role, `text` (stored raw for staff context; access-controlled), `intent`, `citations` JSON, `safety` JSON, `llm_call_ids` JSON, `created_at`
  - `chat_handoffs`: id, session_id, reason, `summary_redacted`, status (open, claimed, closed), claimed_by, timestamps, version
- **Access.** Logged-in patients own their sessions. Anonymous visitors get a random `session_token` once; only its hash is stored. Any other caller gets **404** (no existence leak). Front desk can read sessions **only through a handoff** they can see.
- **Limits.** Message ≤ 2,000 characters; ≤ 30 turns per session; a per-session cost budget of `chat_session_budget_usd = 0.05` (brief improvement 12); a rate limit of 20/min per user or IP; and the Phase 2 daily and monthly budget still applies on top.
- **Alert dedup** (Phase 2 carry-over): the same session or patient, the same red-flag codes, within 10 minutes → reuse the open alert (the response still shows the script). The same applies to the retried-emergency booking case.

### 3.11 Prompt injection
- **In chat:** layer 1 doesn't care, and the LLM prompts treat user text as data. The answer post-checks block policy leaks. An injection pattern list ("ignore (all|previous) instructions", "you are now", "system prompt", "list all patients") logs `prompt_injection_suspected` (§9) and continues normally.
- **In KB documents:** ingestion **scans every chunk** with the same pattern list and refuses to index flagged chunks unless the doc is marked `trusted_override: true`. This shows in the ingest report. Tests use a fixture KB containing a poisoned doc.
- The LLM never gets tools or DB access in this phase; only retrieved public KB text and the redacted conversation.

### 3.12 Observability
One Opik trace per chat turn, with child spans:
- `guardrails` (level, codes)
- `router` (intent, confidence)
- `retrieve` (BM25 and vector top ids with scores, fused ids, rerank scores, confidence; **chunk ids only**)
- `generate` (an LLM span from LLMClient)
- `postcheck` (flags)

The patient's text is redacted with `redact_all` before it goes into any span or log.

### 3.13 Evals in this phase (Phase 5 scales them up)
| Suite | Live? | Gate | Content |
|---|---|---|---|
| `rag_retrieval` | offline (real embeddings, model cached in CI) | **CI gate** | ≥ 40 questions with expected `slug`s: typos, doctor misspellings, Hinglish, follow-up rewrites given as standalone, **≥ 8 out-of-KB questions** (expected: refuse), and the price conflict (expected: v2 only). Metrics: recall@5, MRR, `superseded_leak` (must be 0), `out_of_kb_refusal`. |
| `router_rules` | offline | **CI gate** | ≥ 40 messages through layer 1 only: hidden emergencies, medical questions, lab-report pastes, human requests, empty/emoji/spam. Metric `exact_intent` on the cases layer 1 must decide, and **emergency recall 1.0**. |
| `router_llm` | live | report | ≥ 60 messages over all 8 intents (incl. rf-026-style booking chatter); accuracy and a confusion matrix in the report. |
| `rag_answer_live` | live | report | ≥ 25 questions: citation validity, number faithfulness (no number missing from cited chunks), refusal on out-of-KB, the latest price used, injected-doc ignored, zero advice-filter hits. (The LLM judge comes in Phase 5.) |

### 3.14 UI
- **Patient → "Ask NetraCare"** (`st.chat_message`):
  - inline `[n]` markers with a **Sources** expander per answer (doc title, section, effective date, excerpt, resolved via `/kb/chunks/{id}`)
  - the §10 chat footer always visible
  - emergency replies in a red `st.error` box
  - booking suggestions as a button to the Book page with the specialty and doctor prefilled
  - a "Talk to a person" button
- **Anonymous chat** is reachable from the login page ("Questions? Ask NetraCare").
- **Front desk → "Chat handoffs"**: open handoffs with the redacted summary; claim, view the conversation, close.
- **Admin → "Knowledge base"**: documents with their version, effective date, current/superseded and chunk count; index fingerprint and status; a **Re-index** button (`POST /admin/kb/reindex`); a debug search box showing BM25, vector and fused ranks.

## 4. Corner cases → where handled → test
| Case | Handling | Test / eval |
|---|---|---|
| "lol bleach in my eye, anyway how much is LASIK?" | Layer 1 emergency short-circuits; no RAG | `router_rules`, API test |
| Not in KB ("Do you do hair transplants?", "parking fee in 2019?") | confidence gate → §5.4 verbatim | `rag_retrieval` out-of-KB, API test |
| Old vs new price | `is_current` filter; 2026 list cited with its date | `superseded_leak=0`, live answer |
| "Dr Shetti timings", "lasic cost" | fuzzy doctor + typo map + semantic | retrieval eval |
| "Which drops for dry eye?" | layer 1 → §5.3 + Cornea routing | `router_rules` |
| Pasted report "IOP 24 / 22, is that bad?" | layer 1 (lab pattern) → §5.3 "test results" + Glaucoma routing | `router_rules` |
| "My 6 year old squints" | `mentions_child` → D4 template | unit + rules eval |
| "Blurry and itchy and pressure" (multi-specialty) | one clarifying question, then D5 | service test with 2 turns |
| Empty, emoji, 5,000 characters, spam | fixed reply / 422 / end session | API tests |
| Injection in chat | data delimiters, post-checks, event log | unit + live eval |
| Injection inside a KB doc | ingest refuses the chunk | ingestion test (fixture KB) |
| LLM down or over budget | router keyword fallback; extractive answer with citations | service tests with `DisabledProvider` |
| Answer invents a price | number post-check → repair → extractive fallback | unit test with `FakeProvider` |
| Another user's session id | 404 | permission matrix |
| Repeated emergency in a session | alert dedup | service test |

## 5. Risks
| Risk | Mitigation |
|---|---|
| Model download (~130 MB bge + ~90 MB reranker) on first run / in CI | `models/` cache dir (git-ignored), `dev.ps1 models` pre-download, CI `actions/cache`; unit tests use a deterministic `HashingEmbedder` and never download |
| chromadb's large dependency tree / Windows quirks | Pin it; `VectorStore` protocol keeps an in-memory fallback; clean-venv install check as in every phase |
| A threshold that is too strict → useless bot; too loose → hallucination | Calibrated on eval data, both gates (score + `answerable`), plus the numeric post-check |
| Small KB → recall looks great but won't generalise | Phase 5 grows the dataset to ≥ 50 RAG Q&A; report per-category recall |
| Groq rate limits during live evals | Sequential live runs, Phase 2's retry/backoff, reuse scored outputs on upload |
| Cost growth from 2 calls per turn (router + answer) | Layer 1 short-circuits; no answer call below the threshold; per-session budget; `dev.ps1` usage report |

## 6. Open questions (defaults assumed, non-blocking)
- Should anonymous (not logged-in) visitors use the chat? *Assume yes, with a stricter rate limit (10/min/IP) and no handoff callbacks (the front-desk number is shown instead).*
- How long to keep chat transcripts? *Assume 90 days, with a purge CLI. A full retention policy is a Phase 7 candidate.*
- Rerank on by default? *Decided by the k/rerank sweep (T7); the default is on if it improves MRR at acceptable latency (p95 < 300 ms on CPU).*

## Decisions Log
- **2026-11-22** - (approved 2026-11-24, pending approval) Use **fastembed (ONNX)** instead of sentence-transformers. The models are the same (`BAAI/bge-small-en-v1.5`; reranker `Xenova/ms-marco-MiniLM-L-6-v2`), and fastembed avoids torch, which helps Render in Phase 7.
- **2026-11-22** - (approved 2026-11-24) `kb_chunks` table as the citation source of truth; BM25 in memory; Chroma for vectors only, with fingerprinted collections.
- **2026-11-22** - (approved 2026-11-24) Superseded docs are excluded at retrieval time, so the latest effective date wins deterministically.
- **2026-11-22** - (approved 2026-11-24) Two-layer router: deterministic safety layer first (emergency, medical question, lab report, human request), then the LLM router, which also rewrites follow-ups into standalone queries.
- **2026-11-22** - (approved 2026-11-24) No answer LLM call below the retrieval-confidence threshold; extractive fallback whenever the LLM is unavailable or fails the post-checks.
- **2026-11-24** - Plan approved by the user, including the open-question defaults:
  - logged-out visitors can use the chat (stricter rate limit; the front-desk number is shown instead of a callback);
  - chat transcripts are kept for 90 days (purge CLI);
  - the cross-encoder rerank stays on only if the T6 sweep shows it helps.
- **2026-11-24** - Deviations found during T1–T4:
  - **Collection naming.** The Chroma collection is named per (model, chunker version), e.g. `kb_baai_bge_small_en_v1_5_c1`, and the content fingerprint is stored in the collection's metadata rather than its name. A fingerprint in the name would force a full re-embed on every document edit, which defeats incremental ingestion. A stale store is still detected by comparing the fingerprint and the id set with `kb_index_state`, and then fully rebuilt.
  - **Reseed.** `dev.ps1 reseed` / `make reseed` run `python -m netracare.rag.ingest --force` after seeding, instead of a `--with-index` flag on the seed runner. This keeps the seed runner free of model dependencies, so seed tests stay fast.
  - **Session budget.** The existing `conversation_budget_usd` setting ($0.05, from Phase 0) is reused as the per-session chat budget; no new setting was added.
  - **Ingestion transactions.** The Ingestor plans and embeds before opening its write transaction, which lasts milliseconds, and updates the vector store after the commit. If the vector update fails, the next run detects the mismatch and rebuilds.
  - **Smoke test (this laptop).** The first model download took about 40 s per model (152 MB in total). With the models cached, the embedder and the reranker each load in about 2.2–2.6 s. The real KB gives 23 documents → 77 chunks, the largest 245 tokens.
- **2026-11-24** - **T6 sweep results** (`scripts/rag_sweep.py`, dataset `rag_retrieval_v0`: 46 answerable and 10 out-of-KB questions; report `reports/evals/rag_sweep-20261124T104536.md`):

  | config | recall@1 | @3 | @5 | MRR@5 | p50 | p95 |
  |---|---|---|---|---|---|---|
  | hybrid, no rerank | 0.913 | 1.000 | 1.000 | 0.949 | 34 ms | 51 ms |
  | hybrid + rerank top-5 | 0.978 | 1.000 | 1.000 | 0.989 | 339 ms | 1,162 ms |
  | hybrid + rerank top-10 | 0.978 | 1.000 | 1.000 | 0.989 | 743 ms | 1,992 ms |

  - **Rerank off** (per the user's rule: it stays on only if tests show it helps). It improves only the order inside the top 3–5, and the answer step reads all five chunks, so recall@3 and recall@5, the numbers that matter, are 1.0 either way. It costs 0.3–2 s per query on this CPU. As a confidence signal it is worse: the MS-MARCO reranker gives Hinglish and paraphrased questions near-zero scores, so no threshold keeps in-KB answerable ≥ 0.95. `rag_rerank=false` (it can still be enabled; then `rag_rerank_k=5`).
  - **k = 5** (recall@3 is already 1.0; 5 gives headroom for the larger Phase 5 dataset).
  - **Threshold 0.605** on the top vector cosine, with the bge query prefix (the prefix separated the two groups slightly better than no prefix): in-KB answerable 0.978, out-of-KB refused 0.90. The margin is narrow: "What is the price of a knee replacement?" scores 0.604 and "what are your opening hours" 0.609. "Do you offer hair transplants?" (0.626) passes the gate, so for such questions the LLM's `answerable=false` is the second, designed gate. The baseline floors for the two threshold-dependent metrics are the plan targets (0.85 / 0.95), not the measured values. Phase 5 recalibrates on ≥ 50 RAG questions.
  - BM25 "lexical support" was tested as an extra confidence signal and rejected: price words make off-topic price questions ("dental cleaning cost") match as strongly as real ones.
  - `hypothesis` is not a dependency (it wasn't in Phase 2 either). The RRF property tests use 25 seeded random cases.
  - Renamed `IndexStale` to `IndexStaleError` (lint rule N818).
- **2026-11-24** - **Safety-rule refinement (E7).** The Phase 2 rule flagged *any* drooping eyelid as an E7 emergency. Policy 4.1 E7 is about *sudden* drooping or nerve signs, and `kb/procedures/ptosis.md` treats a droopy lid as a routine Cataract & General Ophthalmology / oculoplasty consult. In chat this sent routine questions ("I have a droopy eyelid, who should I see?") to the emergency script.
  - **Change:** a droop now raises E7 only with "sudden", a nerve sign (numbness, weakness, slurring) or a headache. Double vision still raises E7 on its own.
  - **Evals:** three boundary cases added to `safety_redflags_v0` (rf-033 long-standing droop → routine; rf-034 sudden droop → E7; rf-035 droop with facial numbness → E7). `safety_redflags_rules`: emergency recall 1.0, urgent recall 1.0, no under-triage 1.0; baseline refreshed (35 cases).
  - **Known gap (unchanged from Phase 2):** facial droop with slurred speech and no eye mention gives no flag. These are stroke signs, not an eye red flag in policy 4.1. Noted for Phase 5's red-team set.
- **2026-11-24** - T10 implementation notes:
  - **Session budget → no-LLM mode** instead of a handoff. A conversation that reaches `conversation_budget_usd` keeps working without the LLM: rules, keyword router, extractive cited answers. This is safe and costs nothing, and no front-desk work is created. `HandoffReason.BUDGET` exists for a later manual use.
  - **Triage inside chat.** The full Phase 2 triage (rules + LLM classifier, alert, dedup) runs when layer 1 flags a red flag, or when the router reports a symptom (a concern phrase, a booking or a medical question, plus clinical words) or says "emergency". A router "emergency" is floored at **urgent** (policy 4.4: when layers disagree, use the more severe level). Pricing and FAQ questions skip the extra triage call.
  - **Alert dedup.** Same owner (patient, else chat session) + same rule codes + open + within `ALERT_DEDUP_MINUTES` → reuse. This covers repeated chat emergencies and retried emergency bookings (Phase 2 carry-over, now fixed in `BookingService._triage_reason`).
  - **`chat_messages.seq`** was added (unique per session) because timestamps can tie, and with a frozen test clock they always do.
  - **Spam** ends the session and creates a `spam` handoff (the staff flag) for anonymous sessions too. Other handoffs need a signed-in patient; anonymous visitors get the front-desk number.
  - **Frustration:** 2 negative turns create a handoff (signed-in) or offer the number (anonymous).
- **2026-11-24** - **T13 live findings and fixes:**
  - **Citations.** gpt-oss-120b reliably fills the `citations` field but rarely writes inline `[n]` markers. On the first live run every answer failed `no_citation` and fell back to extractive mode. `with_markers` now appends the markers from the `citations` field before the post-checks, so invalid numbers and ungrounded numbers are still rejected. Second run: 25/26 pass; `no_advice`, `numbers_grounded`, `citation_valid`, `refused_when_expected` and `expected_text` are all 1.0. The "Is LASIK still 45,000?" answer was caught by the number check and repaired.
  - **Medical-question detector over-refused logistics** (Phase 2 `asks_medical_advice`). "Do I need someone to drive me after dilating drops?", "When should I stop wearing lenses before the LASIK evaluation?" and "Do I need to fast before cataract surgery?" got the 5.3 refusal.
    - Narrowed: "do I need" counts as medical only when followed by surgery, glasses, drops, medicine or treatment; "should I stop/start…" needs a medicine word; preparation context ("before the evaluation/surgery/visit") without a medicine word is not medical.
    - Four boundary cases added to `router_rules_v0` (55 cases, still 1.0). `safety_redflags_rules` is unchanged: emergency recall 1.0, no under-triage 1.0.
  - **Router rewrite vs threshold.** "what are your opening hours" scored 0.592 after the router rewrote it (0.609 as typed). For a first message whose rewrite falls below the threshold, retrieval retries with the patient's own words.
  - **Known residual cases (for Phase 5's prompt tuning):**
    - "Do I need to fast before cataract surgery?" is still classed as a medical question by the LLM router or triage. This is a safe over-refusal: routing plus a booking offer.
    - Router borderlines: privacy → off-topic, "Dr Shetti timings" → faq, "what can you do?" → off-topic.
    - "Do you offer hair transplants?" is routed `off_topic` (the front-desk number) rather than given the 5.4 script. Both are safe.
  - The smoke script's console output is now encoding-safe: the model can return U+202F, which cp1252 cannot print.
