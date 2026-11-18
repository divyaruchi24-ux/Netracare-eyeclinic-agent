# syntax=docker/dockerfile:1
FROM python:3.13-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt ./
RUN pip install -r requirements.txt

COPY pyproject.toml README.md alembic.ini ./
COPY src ./src
COPY seed ./seed
COPY kb ./kb
RUN pip install --no-deps .

RUN useradd --create-home --uid 10001 netracare && mkdir -p /app/data && chown -R netracare /app/data
USER netracare

EXPOSE 8000
CMD ["sh", "-c", "alembic upgrade head && uvicorn netracare.api.main:app --host 0.0.0.0 --port 8000"]
