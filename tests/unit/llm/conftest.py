from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from netracare.config import Settings
from netracare.db.session import make_session_factory
from netracare.llm.client import LLMClient
from netracare.llm.pricing import Pricing
from netracare.llm.prompts import PromptRegistry
from netracare.llm.providers import FakeProvider, LLMProvider
from tests.conftest import ROOT
from tests.helpers import MutableClock

IST = ZoneInfo("Asia/Kolkata")


def llm_test_settings(**overrides: object) -> Settings:
    base: dict[str, object] = {"groq_api_key": "test-key", "opik_enabled": False}
    base.update(overrides)
    return Settings(_env_file=None, **base)  # type: ignore[call-arg, arg-type]


@pytest.fixture
def llm_settings() -> Settings:
    return llm_test_settings()


@pytest.fixture
def llm_clock() -> MutableClock:
    return MutableClock(datetime(2026, 11, 10, 9, 0, tzinfo=IST))


@pytest.fixture
def factory(migrated_engine: Engine) -> sessionmaker[Session]:
    return make_session_factory(migrated_engine)


@pytest.fixture
def registry() -> PromptRegistry:
    return PromptRegistry(ROOT / "prompts").load()


@pytest.fixture
def pricing() -> Pricing:
    return Pricing.load(ROOT / "config" / "model_pricing.yaml")


@pytest.fixture
def make_client(
    llm_settings: Settings,
    llm_clock: MutableClock,
    registry: PromptRegistry,
    pricing: Pricing,
    factory: sessionmaker[Session],
) -> Callable[..., LLMClient]:
    def _make(provider: LLMProvider | None = None, settings: Settings | None = None) -> LLMClient:
        return LLMClient(
            settings=settings or llm_settings,
            clock=llm_clock,
            provider=provider or FakeProvider(),
            registry=registry,
            pricing=pricing,
            session_factory=factory,
            sleep=lambda _s: None,
        )

    return _make


def write_prompt(root: Path, pid: str, version: int, body: str) -> None:
    d = root / pid
    d.mkdir(parents=True, exist_ok=True)
    (d / f"v{version}.yaml").write_text(body, encoding="utf-8")
