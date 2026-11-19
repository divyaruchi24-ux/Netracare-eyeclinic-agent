<#
.SYNOPSIS
  Windows task runner (equivalent of the Makefile).
.EXAMPLE
  .\scripts\dev.ps1 check
#>
param([Parameter(Position = 0)][string]$Task = "help")

$ErrorActionPreference = "Stop"
$env:UV_LINK_MODE = "copy"
Set-Location (Split-Path $PSScriptRoot -Parent)

# Override ports when 8000/8501 are taken by another app:  $env:API_PORT=8001; $env:UI_PORT=8502
$ApiPort = if ($env:API_PORT) { $env:API_PORT } else { "8000" }
$UiPort = if ($env:UI_PORT) { $env:UI_PORT } else { "8501" }

function Uv { python -m uv @args; if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE } }

switch ($Task) {
    "install" { Uv sync }
    "lint" { Uv run ruff check .; Uv run ruff format --check . }
    "fmt" { Uv run ruff check --fix .; Uv run ruff format . }
    "type" { Uv run mypy }
    "test" { Uv run pytest --cov=netracare --cov-report=term-missing }
    "check" {
        Uv run ruff check .; Uv run ruff format --check .
        Uv run mypy
        Uv run pytest --cov=netracare --cov-report=term-missing
    }
    "migrate" { Uv run alembic upgrade head }
    "seed" { Uv run python -m netracare.seed --summary }
    "reseed" { Uv run python -m netracare.seed --reset --summary }
    "api" { Uv run uvicorn --factory netracare.api.main:app_factory --reload --port $ApiPort }
    "ui" {
        if (-not $env:API_BASE_URL) { $env:API_BASE_URL = "http://localhost:$ApiPort" }
        Uv run streamlit run src/netracare/ui/app.py --server.port $UiPort
    }
    "evals" { Uv run python -m netracare.evals run all --fail-under }
    "evals-upload" { Uv run python -m netracare.evals run booking_policy --upload --fail-under }
    "evals-live" {
        Uv run python -m netracare.evals run safety_redflags_llm --live --upload
        Uv run python -m netracare.evals run visit_summary_live --live --upload
    }
    "groq-smoke" { Uv run python scripts/groq_smoke.py }
    "requirements" {
        Uv export --no-dev --no-hashes --no-emit-project --format requirements-txt -o requirements.txt
        Uv export --only-group dev --no-hashes --no-emit-project --format requirements-txt -o requirements-dev.txt
    }
    default {
        Write-Host "Tasks: install lint fmt type test check migrate seed reseed api ui evals evals-upload evals-live groq-smoke requirements"
    }
}
