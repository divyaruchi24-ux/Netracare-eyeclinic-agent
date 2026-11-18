.PHONY: install lint fmt type test check migrate seed api ui evals evals-upload requirements

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
	uv run uvicorn --factory netracare.api.main:app_factory --reload --port 8000

ui:
	uv run streamlit run src/netracare/ui/app.py --server.port 8501

evals:
	uv run python -m netracare.evals run all --fail-under

evals-upload:
	uv run python -m netracare.evals run booking_policy --upload --fail-under

requirements:
	uv export --no-dev --no-hashes --no-emit-project --format requirements-txt -o requirements.txt
	uv export --only-group dev --no-hashes --no-emit-project --format requirements-txt -o requirements-dev.txt
