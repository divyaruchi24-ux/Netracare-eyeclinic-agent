# NetraCare

AI front desk & care coordination agent for **NetraCare**, a *fictional* 5-doctor eye clinic.

> All clinic, doctor and patient data in this repository is fictional. The assistant never gives medical advice — see [docs/safety_policy.md](docs/safety_policy.md).

## Quick start

```powershell
python -m pip install --user uv
.\scripts\dev.ps1 install   # uv sync
copy .env.example .env      # then fill in secrets (never commit .env)
.\scripts\dev.ps1 models    # once: download the embedding models (~150 MB) into ./models
.\scripts\dev.ps1 reseed    # fresh DB: migrate + demo data + index the knowledge base
.\scripts\dev.ps1 check     # ruff + mypy + pytest
.\scripts\dev.ps1 api       # http://localhost:8000/docs
.\scripts\dev.ps1 ui        # http://localhost:8501 (Streamlit; talks to the API)
.\scripts\dev.ps1 evals     # offline eval suites + regression gate
.\scripts\dev.ps1 groq-smoke # live Groq check (needs GROQ_API_KEY)
```

Demo logins use `SEED_DEFAULT_PASSWORD`: `admin@netracare.example`, `frontdesk1@netracare.example`, and `kavya.nair@netracare.example` (a doctor). You can also register a new patient in the UI.

**AI features (Groq):** set `GROQ_API_KEY` in `.env` for the red-flag classifier, the daily eye-fact banner and visit-summary drafts. Without a key everything still works safely: triage runs on the deterministic rules, the banner shows a reviewed static fact, and doctors write summaries by hand. Spend is capped at `DAILY_LLM_BUDGET_USD` (1.50) and `MONTHLY_LLM_BUDGET_USD` (20); prices live in `config/model_pricing.yaml`, prompts in `prompts/` (`python -m netracare.llm.prompts list`). Admins see usage under **AI usage**.

**Patient chat (Ask NetraCare):** answers questions from the clinic's own documents in `kb/` with numbered sources. Retrieval is hybrid: BM25 keyword search plus `BAAI/bge-small-en-v1.5` embeddings (fastembed, CPU, no torch) in Chroma, fused with Reciprocal Rank Fusion. Superseded documents, such as last year's price list, are never cited. Questions the knowledge base can't answer get an honest "I don't know" and a front-desk handoff. Medical questions get the safety policy's refusal and the right specialist, and emergencies get the verbatim escalation script, before any AI call. Logged-out visitors can chat too. After editing `kb/`, run `.\scripts\dev.ps1 ingest` or use **Knowledge base → Re-index** as admin. Transcripts older than 90 days are removed with `python -m netracare.services.chat_retention purge`.

**Opik:** to trace bookings and approvals (with no patient details), set `OPIK_API_KEY` and `OPIK_ENABLED=true`; `OPIK_WORKSPACE` is optional. Run `.\scripts\dev.ps1 evals-upload` to log an experiment. See [docs/evals.md](docs/evals.md).

If ports 8000/8501 are taken, set `$env:API_PORT="8001"; $env:UI_PORT="8502"` before `api` / `ui`.

Docker is **optional**. `docker compose up --build` works where Docker is available, and CI builds the image, but nothing requires it.

On Linux/macOS use the equivalent `make` targets.

Planning docs live in [docs/](docs/) (global and per-phase `understanding.md`, `plan.md`, `task.md`).
