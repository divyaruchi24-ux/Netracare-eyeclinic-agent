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
    "api" { Uv run uvicorn netracare.api.main:app --reload --port 8000 }
    "requirements" {
        Uv export --no-dev --no-hashes --no-emit-project --format requirements-txt -o requirements.txt
        Uv export --only-group dev --no-hashes --no-emit-project --format requirements-txt -o requirements-dev.txt
    }
    default {
        Write-Host "Tasks: install lint fmt type test check migrate seed reseed api requirements"
    }
}
