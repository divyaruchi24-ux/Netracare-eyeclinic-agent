.PHONY: install lint fmt type test check migrate seed api requirements

install:
	uv sync

lint:
	uv run ruff check .
	uv run ruff format --check .

fmt:
	uv run ruff check --fix .
	uv run ruff format .

type:
	uv run mypy

test:
	uv run pytest --cov=netracare --cov-report=term-missing

check: lint type test

migrate:
	uv run alembic upgrade head

seed:
	uv run python -m netracare.seed --summary

api:
	uv run uvicorn netracare.api.main:app --reload --port 8000

requirements:
	uv export --no-dev --no-hashes --no-emit-project --format requirements-txt -o requirements.txt
	uv export --only-group dev --no-hashes --no-emit-project --format requirements-txt -o requirements-dev.txt
