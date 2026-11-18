# NetraCare

AI front desk & care coordination agent for **NetraCare**, a *fictional* 5-doctor eye clinic.

> All clinic, doctor and patient data in this repository is fictional. The assistant never gives medical advice — see [docs/safety_policy.md](docs/safety_policy.md).

## Quick start (Phase 0)

```powershell
python -m pip install --user uv
.\scripts\dev.ps1 install   # uv sync
copy .env.example .env      # then fill in secrets
.\scripts\dev.ps1 migrate   # alembic upgrade head
.\scripts\dev.ps1 seed      # demo data
.\scripts\dev.ps1 check     # ruff + mypy + pytest
.\scripts\dev.ps1 api       # http://localhost:8000/health
```

On Linux/macOS use the equivalent `make` targets.

Planning docs live in [docs/](docs/) (global and per-phase `understanding.md`, `plan.md`, `task.md`).
