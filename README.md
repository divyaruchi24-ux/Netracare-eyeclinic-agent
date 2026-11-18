# NetraCare

AI front desk & care coordination agent for **NetraCare**, a *fictional* 5-doctor eye clinic.

> All clinic, doctor and patient data in this repository is fictional. The assistant never gives medical advice — see [docs/safety_policy.md](docs/safety_policy.md).

## Quick start

```powershell
python -m pip install --user uv
.\scripts\dev.ps1 install   # uv sync
copy .env.example .env      # then fill in secrets (never commit .env)
.\scripts\dev.ps1 reseed    # fresh DB: migrate + demo data
.\scripts\dev.ps1 check     # ruff + mypy + pytest
.\scripts\dev.ps1 api       # http://localhost:8000/docs
.\scripts\dev.ps1 ui        # http://localhost:8501 (Streamlit; talks to the API)
.\scripts\dev.ps1 evals     # offline eval suites + regression gate
```

Demo logins use `SEED_DEFAULT_PASSWORD`: `admin@netracare.example`, `frontdesk1@netracare.example`, and `kavya.nair@netracare.example` (a doctor). You can also register a new patient in the UI.

**Opik:** to trace bookings and approvals (with no patient details), set `OPIK_API_KEY` and `OPIK_ENABLED=true`; `OPIK_WORKSPACE` is optional. Run `.\scripts\dev.ps1 evals-upload` to log an experiment. See [docs/evals.md](docs/evals.md).

If ports 8000/8501 are taken, set `$env:API_PORT="8001"; $env:UI_PORT="8502"` before `api` / `ui`.

Docker is **optional**. `docker compose up --build` works where Docker is available, and CI builds the image, but nothing requires it.

On Linux/macOS use the equivalent `make` targets.

Planning docs live in [docs/](docs/) (global and per-phase `understanding.md`, `plan.md`, `task.md`).
