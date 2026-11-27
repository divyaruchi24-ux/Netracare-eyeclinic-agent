"""Opt-in live booking-agent test against Groq: `RUN_LIVE=1 pytest -m live tests/live`."""

from __future__ import annotations

import os

import pytest

from netracare.config import Settings
from tests.conftest import ROOT

pytestmark = pytest.mark.live

_SETTINGS = Settings(_env_file=ROOT / ".env", opik_enabled=False)  # type: ignore[call-arg]
_KEY = _SETTINGS.groq_api_key.get_secret_value() if _SETTINGS.groq_api_key else ""
if os.environ.get("RUN_LIVE") != "1" or not _KEY:
    pytest.skip("live tests need RUN_LIVE=1 and GROQ_API_KEY", allow_module_level=True)


def test_live_booking_conversation_books_only_after_yes() -> None:
    from netracare.evals.booking_tasks import booking_agent_live_task

    out = booking_agent_live_task(
        {
            "turns": [
                "I'd like a check-up with Dr Kavya Nair next Friday morning",
                "the second one",
                "yes but make it 11am",
                "first",
                "yes",
            ],
            "patient": "P010",
        }
    )
    assert out["write_safe"] and out["writes_ok"] == ["book_appointment"]
    assert out["final_kind"] == "confirmed"
