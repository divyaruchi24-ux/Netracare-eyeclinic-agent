from __future__ import annotations

import os

# Tests never talk to Opik (the developer's .env may hold a real key).
os.environ["OPIK_ENABLED"] = "false"
os.environ["OPIK_TRACK_DISABLE"] = "true"

from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from netracare.db.session import make_engine, make_session_factory

ROOT = Path(__file__).resolve().parents[1]


def alembic_config(url: str) -> Config:
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", url)
    return cfg


@pytest.fixture
def db_url(tmp_path: Path) -> str:
    return f"sqlite:///{(tmp_path / 'test.db').as_posix()}"


@pytest.fixture
def migrated_engine(db_url: str) -> Iterator[Engine]:
    command.upgrade(alembic_config(db_url), "head")
    engine = make_engine(db_url)
    yield engine
    engine.dispose()


@pytest.fixture
def session(migrated_engine: Engine) -> Iterator[Session]:
    factory = make_session_factory(migrated_engine)
    s = factory()
    try:
        yield s
    finally:
        s.rollback()
        s.close()
