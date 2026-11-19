"""Opt-in live tests against Groq: `RUN_LIVE=1 pytest -m live tests/live`.

Skipped unless RUN_LIVE=1 and GROQ_API_KEY is set (in the environment or .env).
"""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path

import pytest
from sqlalchemy import Engine

from netracare.config import Settings
from netracare.db.session import make_session_factory
from netracare.domain.clock import SystemClock
from netracare.llm.client import LLMClient
from netracare.llm.pricing import Pricing
from netracare.llm.prompts import PromptRegistry
from netracare.llm.providers import GroqProvider
from netracare.llm.schemas import TriageLLMOutput, VisitSummaryDraft
from tests.conftest import ROOT

pytestmark = pytest.mark.live

_SETTINGS = Settings(_env_file=ROOT / ".env", opik_enabled=False)  # type: ignore[call-arg]
_KEY = _SETTINGS.groq_api_key.get_secret_value() if _SETTINGS.groq_api_key else ""
if os.environ.get("RUN_LIVE") != "1" or not _KEY:
    pytest.skip("live tests need RUN_LIVE=1 and GROQ_API_KEY", allow_module_level=True)


@pytest.fixture
def live_client(migrated_engine: Engine) -> Callable[[], LLMClient]:
    def make() -> LLMClient:
        return LLMClient(
            settings=_SETTINGS,
            clock=SystemClock(),
            provider=GroqProvider(_KEY),
            registry=PromptRegistry(Path(ROOT / "prompts")).load(),
            pricing=Pricing.load(ROOT / "config/model_pricing.yaml"),
            session_factory=make_session_factory(migrated_engine),
        )

    return make


def test_fast_model_structured_round_trip(live_client: Callable[[], LLMClient]) -> None:
    result = live_client().generate(
        "red_flag_classifier", {"text": "bleach splashed in my eye"}, purpose="live_test"
    )
    assert isinstance(result.value, TriageLLMOutput)
    assert result.value.level.value == "emergency"
    assert result.cost_usd > 0 and result.model.startswith("openai/gpt-oss-20b")


def test_reasoning_model_structured_round_trip(live_client: Callable[[], LLMClient]) -> None:
    result = live_client().generate(
        "visit_summary",
        {"notes": "VA 6/6 both eyes. IOP 15 mmHg OU. Review in 12 months."},
        purpose="live_test",
    )
    assert isinstance(result.value, VisitSummaryDraft)
    assert result.model.startswith("openai/gpt-oss-120b")
