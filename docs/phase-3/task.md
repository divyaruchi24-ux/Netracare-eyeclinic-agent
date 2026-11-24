# Phase 3: Tasks (exact changes)

Paths are relative to the repo root. Verify each group with `.\scripts\dev.ps1 check`.
- Unit and API tests use `FakeProvider` for the LLM and `HashingEmbedder` + `InMemoryVectorStore` for retrieval, so they need no network or model downloads.
- Tests that use the real embedding models are marked `models`. They run by default once the models are cached, and CI caches them.
- Tests that hit Groq are marked `live` and skipped unless `RUN_LIVE=1`.

## T0. Planning
- [x] `docs/phase-3/understanding.md`, `plan.md`, `task.md`
- [x] User approval: "Approve Phase 3 plan?" (2026-11-24)

## T1. Foundations
- [x] `uv add chromadb rank-bm25 fastembed pypdf` (pinned through the lock file). Check that the lock contains no torch.
- [x] `config.py` (and `.env.example`, keys empty):
  - `kb_dir: str = "kb"`
  - `models_cache_dir: str = "./models"`
  - `reranker_model: str = "Xenova/ms-marco-MiniLM-L-6-v2"` (renamed value)
  - `rag_bm25_k: int = 20`, `rag_vector_k: int = 20`, `rag_rrf_k: int = 60`, `rag_top_k: int = 5`
  - `rag_rerank: bool = True`, `rag_rerank_k: int = 10`, `rag_min_confidence: float` (placeholder, calibrated in T6)
  - `chat_max_turns: int = 30`, `chat_max_chars: int = 2000`
  - `chat_session_budget_usd: float = 0.05`
  - `chat_rate_limit_per_min: int = 20`, `chat_anon_rate_limit_per_min: int = 10`
  - `chat_history_turns: int = 3`
  - `router_confidence_threshold: float = 0.6`
  - `alert_dedup_minutes: int = 10`
  - `chat_retention_days: int = 90`
- [x] `.gitignore`: `models/`, `data/chroma/`.
- [x] `pyproject.toml`: register the `models` pytest marker; add mypy overrides for `rank_bm25`, `fastembed` and `chromadb` if they lack stubs.
- [x] `scripts/dev.ps1` + `Makefile`: add `models` (pre-download both models into `models_cache_dir`) and `ingest`; make `reseed` run ingestion after seeding.
- [x] Smoke test: embed 2 sentences with bge-small and rerank 2 pairs on this machine; record load time and RAM in RETRO.

## T2. Migration 0004
- [x] `domain/enums.py`:
  - `ChatIntent(faq_rag, doctor_lookup, pricing, booking, medical_question, emergency, human_handoff, chit_chat, off_topic, clarify)`
  - `ChatRole(user, assistant)`
  - `ChatSessionStatus(active, ended, handoff)`
  - `HandoffStatus(open, claimed, closed)`
  - `HandoffReason(user_request, low_confidence, router_uncertain, frustration, spam, budget)`
- [x] `db/models.py`:
  - **`KbChunk`**: `id` (str, `sha256(slug@version#ordinal)[:32]`), `doc_id` FK, `ordinal`, `heading_path`, `text` Text, `embed_text` Text, `token_count`, `doc_type`, `slug`, `version`, `effective_date`, `doctor_code?`, `specialty?`, `procedure?`, `is_current` bool (index), `checksum`; unique `(doc_id, ordinal)`
  - **`KbIndexState`**: `id=1`, `fingerprint`, `embedding_model`, `chunker_version`, `n_docs`, `n_chunks`, `built_at`, `report` JSON
  - **`ChatSession`**: `id`, `user_id?` FK, `patient_id?` FK, `anon_token_hash?` (unique), `status`, `turns`, `cost_usd` Numeric(12,6), `pending_clarification` JSON?, `created_at`, `last_active_at` (index), `version`
  - **`ChatMessage`**: `id`, `session_id` FK (index), `role`, `text` Text, `intent?`, `citations` JSON, `safety` JSON, `llm_call_ids` JSON, `created_at`
  - **`ChatHandoff`**: `id`, `session_id` FK, `reason`, `summary_redacted` Text, `status`, `claimed_by?`, `claimed_at?`, `closed_by?`, `closed_at?`, `created_at`, `version`; index `(status, created_at)`
  - `TriageAlert`: add `dedup_key` (String 64, index) and `chat_session_id?` FK
- [x] `alembic revision --autogenerate -m "rag chat" --rev-id 0004`, reviewed by hand; the drift test covers it.
- [x] `tests/db/test_constraints.py`: `test_chunk_unique_per_doc_ordinal`, `test_anon_token_hash_unique`.

## T3. Ingestion (`src/netracare/rag/`)
- [x] `rag/types.py`:
  - `SourceDoc(path, meta, body, checksum, kind: md|pdf)`
  - `Chunk(id, doc_slug, version, ordinal, heading_path, text, embed_text, token_count, meta)`
  - `IngestReport(docs_seen, docs_changed, chunks_written, chunks_deleted, refused: list[(slug, ordinal, reason)], fingerprint, duration_ms)`
- [x] `rag/loaders.py`:
  - `load_markdown(path) -> SourceDoc` (reuses `seed.kb.parse_front_matter`)
  - `load_pdf(path) -> SourceDoc` (pypdf text per page plus the `<file>.meta.yaml` sidecar; missing sidecar → `FrontMatterError`)
  - `scan(kb_dir) -> list[SourceDoc]`
- [x] `rag/chunker.py`: `CHUNKER_VERSION = "1"`, `chunk(doc: SourceDoc, target_tokens=250, max_tokens=400) -> list[Chunk]`
  - heading stack and heading path
  - tables kept whole, or split into row groups with the header repeated
  - paragraph splits with a one-sentence overlap
  - footer disclaimer lines stripped
  - `embed_text = f"{title} › {heading_path}\n\n{text}"`
  - token counting with a whitespace/regex approximation (no tokenizer dependency)
  - PDF heading heuristic
- [x] `rag/injection.py`: `INJECTION_PATTERNS`, `scan_text(text) -> list[str]` (shared with chat layer 1).
- [x] `rag/ingest.py`:
  - `Ingestor(session, embedder, store, settings).run(kb_dir, *, force=False) -> IngestReport`:
    - upsert `knowledge_docs` (reuses `seed.kb.upsert_kb` logic) and `kb_chunks` (diff by checksum)
    - set `is_current` from `superseded_by_id`
    - refuse injected chunks unless `trusted_override`
    - embed changed chunks and upsert/delete them in the vector store
    - compute `fingerprint = sha256(embedding_model, CHUNKER_VERSION, sorted doc checksums)` and write `KbIndexState`
  - CLI `python -m netracare.rag.ingest [--force] [--json]`
- [x] `seed/runner.py`: call the Ingestor after the KB upsert when `--with-index` is passed (`dev.ps1 reseed` passes it).
- [x] Tests `tests/unit/rag/test_loaders.py`, `test_chunker.py`, `test_ingest.py`:
  - every real KB doc chunks with a heading path
  - the price table stays intact
  - no chunk over the max
  - footer stripped
  - PDF fixture (a minimal PDF generated in the test) + sidecar loads; missing sidecar errors
  - re-run is a no-op; editing one doc re-embeds only its chunks; deleting a doc removes its chunks and vectors
  - the poisoned fixture doc's chunk is refused and listed in the report
  - superseded doc chunks have `is_current=False`

## T4. Embeddings, vector store, BM25
- [x] `rag/embeddings.py`:
  - `Embedder` Protocol (`model_id`, `dim`, `embed_documents(list[str])`, `embed_query(str)`)
  - `FastEmbedder(model_id, cache_dir)`: lazy load, thread-safe singleton per model; bge query prefix handled
  - `HashingEmbedder(dim=256)`: deterministic token hashing, normalised; for tests
  - `Reranker` Protocol (`score(query, texts) -> list[float]`), `FastReranker`, `OverlapReranker` (test fake)
- [x] `rag/vector_store.py`:
  - `VectorStore` Protocol (`upsert(ids, vectors, metadatas)`, `delete(ids)`, `query(vector, k, where) -> list[(id, cosine)]`, `count()`, `reset()`)
  - `ChromaVectorStore(path, collection)`: `PersistentClient`, `Settings(anonymized_telemetry=False)`, cosine space, collection `kb_<fingerprint12>`
  - `InMemoryVectorStore`
- [x] `rag/bm25.py`:
  - `tokenize(text)`: lowercase, strip punctuation, a small stopword list, a light plural/suffix strip, and the typo map
  - `TYPO_MAP` (extends Phase 2 `_TYPOS`: lasic, lazik, catract, glacoma, gluacoma, retna, opthalmologist, …)
  - `BM25Index.build(chunks)`, `.search(query, k, where) -> list[(id, score)]` (BM25Okapi; filters applied before ranking)
- [x] Tests:
  - `test_embeddings.py`: hashing determinism and normalisation; `FastEmbedder` dim 384 and similar > dissimilar (`models`)
  - `test_vector_store.py`: same contract for both stores (parametrised); Chroma in `tmp_path`; filter by `is_current` / `doc_type`
  - `test_bm25.py`: typo map ("lasic" finds the LASIK doc), filters, empty query → []

## T5. Hybrid retriever
- [x] `rag/doctor_names.py`: `match_doctor(text, doctors) -> DoctorMatch | None`. Uses `difflib.get_close_matches` on full names and surnames (cutoff 0.8) and handles "Dr", "Dr." and "doctor" prefixes.
- [x] `rag/retriever.py`:
  - `RetrievalFilters(current_only=True, doc_types=None, doctor_code=None, specialty=None)`
  - `RetrievedChunk(id, rank, bm25_rank?, vector_rank?, rrf_score, rerank_score?, cosine?, chunk: KbChunk)`
  - `RetrievalResult(query, normalized_query, chunks, confidence, stages: dict, latency_ms)`
  - `HybridRetriever(session, bm25, store, embedder, reranker|None, settings)`:
    - `.retrieve(query, filters, k=None) -> RetrievalResult`
    - `rrf(rankings, k=60)` is a pure function
    - a doctor match adds a `doctor_code` boost (a fused-rank bonus, not a hard filter)
    - confidence = top rerank score (sigmoid) or top cosine
  - `RetrievalIndex` (app-scoped): builds BM25 from `kb_chunks`, opens the store, checks the fingerprint (mismatch → `IndexStale`, rebuilt by ingest), `reload()` after reindex
- [x] `observability/tracing.py`: `retrieval_span(...)`, which records ids, ranks and scores only.
- [x] Tests `tests/unit/rag/test_rrf.py` (hypothesis: monotonic, ties, a single list equals its own order) and `test_retriever.py`:
  - superseded chunks never returned by default
  - filters respected
  - doctor fuzzy boost ("Dr Shetti" → the D2 bio top-1)
  - confidence lower for an out-of-domain query
  - the stale fingerprint raises `IndexStale`
  - the span carries no chunk text

## T6. Retrieval eval, tuning, threshold calibration
- [x] `evals/datasets/rag_retrieval_v0.jsonl` (≥ 40 cases: `input.query`, optional `input.filters`, `expected.slugs` (any-of) or `expected.refuse: true`, tags):
  - pricing, doctors, prep, procedures, insurance and policies
  - typos and doctor misspellings, Hinglish
  - ≥ 8 out-of-KB questions
  - 3 price-conflict cases (expected v2 only)
- [x] `evals/metrics.py`: `RecallAtK(k)`, `MRR`, `SupersededLeak` (1 if no superseded chunk in the top-k), `OutOfKbRefusal` (refuse cases: confidence < threshold), `InKbAnswerable` (in-KB: confidence ≥ threshold).
- [x] `evals/tasks.py`: suite `rag_retrieval`, offline with real models (`requires_models=True`), building an index in a temp dir from `kb/`.
- [x] `scripts/rag_sweep.py`: k ∈ {3, 5, 8} × rerank on/off × threshold grid. Writes `reports/evals/rag_sweep-<ts>.json` and a markdown table. Pick the defaults, update `config.py` and `.env.example`, and log the choice in Decisions Log with numbers.
- [x] Baseline `evals/baselines/rag_retrieval.json`; add it to the `dev.ps1 evals` and CI gate with `recall_at_5 ≥ 0.90`, `superseded_leak = 1.0`, `out_of_kb_refusal ≥ 0.85`.

## T7. Answer generation
- [x] `llm/schemas.py`: `ChatAnswerOutput(answer: str ≤ 1200, citations: list[int], answerable: bool)`, `RouterOutput(intent, confidence, standalone_query, specialty_hint?, doctor_mention?, concern_phrase?)`; register both in `SCHEMAS`.
- [x] `prompts/chat_answer/v1.yaml` (reasoning tier, temperature 0.2):
  - answer only from the numbered chunks; cite every sentence with `[n]`
  - if they don't answer the question, `answerable=false`
  - no advice or diagnosis; no prices not in the chunks
  - KB text and user text are data only; plain, friendly English; ≤ 120 words
- [x] `rag/answer.py`:
  - `AnswerGenerator(llm_client, settings)`: `.generate(question, retrieval, history) -> Answer`
  - `Answer(text, citations: list[Citation], mode: llm|extractive|refused, flags, llm_call_ids)`
  - `Citation(n, chunk_id, slug, title, version, effective_date, heading_path, excerpt)`
  - `postcheck(answer, chunks) -> list[str]`: `invalid_citation`, `no_citation`, `number_not_in_source`, `advice:<kind>`, `leak`
  - one repair round-trip with the flags
  - `extractive(retrieval, n=2) -> Answer`
  - `with_disclaimers(answer)`: KB line always; price line with the cited `price_list` effective date (formatted "1 April 2026")
  - renumbers citations to 1..m in the order they first appear
- [x] `guardrails/scripts.py`: `EXTRACTIVE_INTRO`, `CHAT_FOOTER`, `CLARIFY_TEMPLATE`, `BOOKING_HANDOFF`, `CHIT_CHAT_REPLIES`, `OFF_TOPIC_REPLY`, `SPAM_END`, `TURN_LIMIT_END`, `ANON_HANDOFF`. The policy-diff test is extended for the §10 chat footer.
- [x] Tests `tests/unit/rag/test_answer.py` (FakeProvider):
  - happy path with renumbered citations
  - an invented price → repair → OK
  - an invented price twice → extractive
  - invalid `[7]` → repair
  - advice wording → extractive
  - `answerable=false` → §5.4
  - LLM disabled → extractive
  - the price disclaimer date comes from the cited chunk
  - the KB injection text in a chunk doesn't change the code path (post-checks still run)

## T8. Router
- [x] `agents/router_rules.py`:
  - `Layer1Decision(intent|None, reason, triage_codes, topic)`
  - `layer1(text, session_state) -> Layer1Decision`:
    - empty, emoji or punctuation only
    - spam (the same normalised text ≥ 3 times)
    - red-flag rules (emergency/urgent)
    - `asks_medical_advice`
    - `LAB_REPORT` pattern (values with units/eye labels, "report says", "prescription", "HbA1c", "IOP", "OD/OS")
    - `HUMAN_REQUEST` pattern
    - injection → flag only (no intent)
- [x] `prompts/intent_router/v1.yaml` (fast tier): the intents with definitions and examples (including booking chatter vs routine clinical, rf-026), `standalone_query` rules, data-only delimiters.
- [x] `agents/router.py`:
  - `Router(llm_client, settings).route(text, history, session_state) -> RouteDecision(intent, confidence, standalone_query, specialty_hint, doctor_mention, concern_phrase, source: rules|llm|keyword_fallback)`
  - `keyword_fallback(text)`
- [x] Datasets:
  - `evals/datasets/router_rules_v0.jsonl` (≥ 40: hidden emergencies in FAQ/booking, drops/pressure/stop-medication, pasted reports, human requests, empty/emoji/spam, injection attempts that must not change the intent)
  - `router_llm_v0.jsonl` (≥ 60 across all intents)
- [x] Suites `router_rules` (offline, gate: `recall_intent_emergency = 1.0`, `exact_intent = 1.0` on the cases layer 1 must decide) and `router_llm` (live). Metric `ExactIntent`; confusion matrix in the report `extra`.
- [x] Tests `tests/unit/agents/test_router_rules.py`, `test_router.py`:
  - layer 1 short-circuits with no LLM request
  - LLM low confidence → `faq_rag` marker
  - LLM down → keyword fallback
  - a history follow-up rewrite is used as the retrieval query
  - the LLM can't override a layer-1 emergency (property test over the red-flag dataset)

## T9. Specialist routing
- [x] `config/specialty_routing.yaml`: specialty codes → concern keywords and phrases (the non-red-flag concern lexicon), with `version` and `reviewed_by`.
- [x] `agents/specialist.py`:
  - `SpecialistRouter(directory_service, lexicon).route(text, codes, llm_hint, concern_phrase, pending) -> RoutingOutcome(kind: specialty|pediatric|general|clarify, specialty, doctor_id, doctor_name, sentence, clarify_options)`
  - order: child → rules codes → lexicon → LLM hint
  - two or more candidates, no pending clarification → clarify (one question from `CLARIFY_TEMPLATE`)
  - still ambiguous on the next turn → general (D5)
  - the doctor comes from `DirectoryService` (active doctor for the specialty); the sentence comes from `routing_sentence` only
- [x] Tests `tests/unit/agents/test_specialist.py`:
  - floaters → Retina / Dr. Vikram Shetty
  - "6 year old squint" → D4 template
  - "itchy and blurry and family glaucoma" → clarify, then answer "not sure" → D5
  - the LLM hint passes through `safe_concern`
  - the sentence always matches a §6 template (regex)

## T10. ChatService, sessions, handoffs, alert dedup
- [x] `services/chat_service.py`:
  - `ChatService(session, llm, retriever, router, specialist, triage, directory, handoffs, settings, clock)`
  - `create_session(actor|None) -> (ChatSession, anon_token|None)`
  - `get_session(id, actor, anon_token) -> ChatSession`: NotFound unless the caller owns it
  - `post_message(session_id, text, actor, anon_token) -> ChatTurn`:
    - limits (chars → 422, turns → `TURN_LIMIT_END`, session budget → handoff with reason `budget`)
    - layer 1 → handlers → layer 2 → handlers
    - persists the user and assistant messages
    - updates `cost_usd` from the LLM results
    - frustration counter (2 negative turns → handoff offer)
    - one Opik turn trace
  - `ChatTurn(reply, intent, citations, safety, handoff_id?, suggested_action?, disclaimers, mode)`
  - all LLM and retrieval work runs via `outside_transaction`
- [x] `services/handoff_service.py`:
  - `HandoffService.create(session, reason)`: summary = the last 6 turns redacted with `redact_all` and trimmed; idempotent per open session
  - `list(status)`, `claim(id, actor, version)`, `close(id, actor, version, note)`, `conversation(id, actor)` (front desk only, via the handoff)
  - audit rows for create, claim and close
- [x] `services/triage_service.py`: `classify(..., dedup_key=None)`. When an open alert with the same `dedup_key` exists within `alert_dedup_minutes`, return it instead of creating a new one. The key is `sha256(session_or_patient + sorted codes)`. The booking path passes `patient_id + codes` (fixes the Phase 2 carry-over).
- [x] `services/chat_retention.py` + CLI `python -m netracare.services.chat_retention purge [--days N]`.
- [x] Tests `tests/unit/services/test_chat_service.py` (FakeProvider + HashingEmbedder over the real `kb/`):
  - each intent path
  - a hidden emergency in an FAQ
  - out-of-KB → §5.4, with no answer request in `FakeProvider.requests`
  - the follow-up rewrite flow
  - the clarification flow across 2 turns
  - turn limit, session budget, spam end, frustration handoff
  - the repeated emergency → one alert
  - the LLM down → extractive and keyword router
  - persisted messages carry citations and intent
  - the Opik recording backend has no raw PII
- [x] Tests `tests/unit/services/test_handoff_service.py`, and additions to `tests/unit/services/test_triage.py` for dedup.

## T11. API
- [x] `api/schemas/chat.py`: `ChatSessionOut`, `ChatMessageIn(text: constr(max=2000))`, `ChatTurnOut`, `CitationOut`, `SuggestedActionOut(type: "book", specialty, doctor_id)`, `HandoffOut`, `KbChunkOut`, `KbDocOut`, `KbStatusOut`, `KbSearchOut`.
- [x] `api/routers/chat.py`:
  - `POST /chat/sessions` (optional auth; returns `session_token` for anonymous callers)
  - `GET /chat/sessions/{id}` (owner; anonymous via the `X-Chat-Token` header)
  - `POST /chat/sessions/{id}/messages` (rate-limited per user or IP; stricter for anonymous)
  - `POST /chat/sessions/{id}/handoff`
- [x] `api/routers/kb.py`:
  - `GET /kb/chunks/{id}` (public: KB content is public; superseded chunks are still resolvable, labelled superseded)
  - `GET /kb/docs` (public list)
- [x] `api/routers/handoffs.py` (front_desk, admin): `GET /chat/handoffs?status=`, `POST /chat/handoffs/{id}/claim`, `POST /chat/handoffs/{id}/close`, `GET /chat/handoffs/{id}/conversation`.
- [x] `api/routers/admin.py`: `GET /admin/kb` (status + docs), `POST /admin/kb/reindex` (runs the Ingestor, then `RetrievalIndex.reload()`; audit row), `GET /admin/kb/search?q=` (debug ranks).
- [x] `api/main.py`:
  - `create_app(..., embedder=None, vector_store=None, reranker=None)`
  - app state `retrieval_index` built lazily at first use
  - `chat_limiter`, `chat_anon_limiter`
  - a stale index at startup logs a warning; chat then uses BM25-only retrieval with a lower confidence cap until reindex, so it never serves wrong vectors
- [x] `tests/fixtures_api.py`: the app uses `HashingEmbedder` + `InMemoryVectorStore`, ingested from the real `kb/` once per session.
- [x] Tests:
  - `tests/api/test_chat.py`: anonymous and authenticated flows, token mismatch → 404, other patient → 404, 2001 chars → 422, rate limit → 429, citations resolve via `/kb/chunks/{id}`, the emergency response contains the verbatim script
  - `test_kb.py`, `test_handoffs.py`, `test_admin_kb.py`
  - `test_permission_matrix.py` rows for every new route

## T12. UI
- [x] `ui/api_client.py`: `create_chat`, `send_chat`, `get_chat`, `request_handoff`, `kb_chunk`, `list_handoffs`, `claim_handoff`, `close_handoff`, `handoff_conversation`, `admin_kb`, `admin_reindex`, `admin_kb_search`. The anonymous token is kept in session state.
- [x] `ui/app_pages/chat.py`:
  - history from the session
  - `st.chat_input` (max 2000 characters)
  - a reply renderer: emergency → `st.error`; refusal and low-confidence styled as info; citations as superscript `[n]` plus a **Sources** expander
  - a suggested-action button → the Book page with the specialty and doctor prefilled through `st.session_state`
  - a "Talk to a person" button
  - the §10 footer always rendered
  - a "New conversation" button
- [x] `ui/navigation.py`:
  - patient "Ask NetraCare"
  - anonymous access (login page link → chat page without auth)
  - front desk "Chat handoffs"
  - admin "Knowledge base"
- [x] `ui/app_pages/frontdesk_handoffs.py`, `ui/app_pages/admin_kb.py` (status, docs table with superseded badges, re-index button, debug search with a rank table).
- [x] `ui/app_pages/patient_book.py`: read the prefilled specialty and doctor from session state.
- [x] Tests `tests/ui/test_chat_pages.py` (AppTest with a stub API client): answer with sources, emergency styling, footer present, anonymous chat reachable, the booking button sets prefill, handoff page claim/close, admin re-index.

## T13. Live evals, smoke test, Opik
- [x] `evals/datasets/rag_answer_live_v0.jsonl` (≥ 25):
  - pricing (incl. the conflict), prep, doctors, insurance
  - out-of-KB questions
  - follow-ups given with history
  - one case retrieving the poisoned fixture doc (run against a fixture KB variant)
- [x] Metrics:
  - `CitationValid`
  - `NumbersGrounded`
  - `NoAdvice` (advice filter on the final text)
  - `RefusedWhenExpected`
  - `LatestPrice` (contains the expected price and not the superseded one)
- [x] Suites `rag_answer_live` and `router_llm` (`requires_live=True`, `prompts=[chat_answer, intent_router]`); usage goes to the eval ledger DB.
- [x] `scripts/groq_smoke.py`: add a chat round trip (a router call and an answer call against a temp index) and a cost check.
- [x] `tests/live/test_chat_live.py`: one FAQ, one out-of-KB and one medical question against real Groq.
- [x] Run both live suites with `--upload`; record the Opik experiment names in RETRO.
- [x] `docs/evals.md`: document the four new suites, gates and metrics.

## T14. Close-out
- [x] `uv export` → `requirements.txt` / `requirements-dev.txt`; clean-venv `pip install -r requirements.txt -r requirements-dev.txt` + `pip check` + import smoke (chromadb, fastembed, rank_bm25, pypdf).
- [x] CI (`.github/workflows/ci.yml`): cache `models/` (key = model ids), `dev.ps1 models` equivalent step before tests and evals; add `rag_retrieval` and `router_rules` to the offline gate and `rag_answer_live` and `router_llm` to `evals-opik` when `GROQ_API_KEY` is set.
- [x] `.\scripts\dev.ps1 check` (ruff, format, mypy strict, pytest + coverage thresholds).
- [x] Live demo per [plan.md](plan.md) against port 8001 with real Groq.
- [x] `docs/phase-3/RETRO.md`; tick this file and `plan.md` DoD; update `docs/task.md`, `docs/understanding.md` Decisions Log, `docs/plan.md` if needed, and README (chat, KB, ingest commands).
- [x] Commit `phase-3: patient chat with hybrid RAG, cited answers, intent router` (identity `divyaruchi24-ux`), push, CI green.
