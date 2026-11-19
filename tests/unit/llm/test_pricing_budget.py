from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from netracare.domain.clock import FixedClock
from netracare.llm.budget import BudgetGuard
from netracare.llm.pricing import Pricing
from netracare.llm.types import BudgetExceeded
from tests.conftest import ROOT
from tests.unit.llm.conftest import llm_test_settings


def test_pricing_file_is_dated_and_covers_configured_models() -> None:
    p = Pricing.load(ROOT / "config" / "model_pricing.yaml")
    assert p.verified_on and p.source_url
    s = llm_test_settings()
    for model in (s.groq_model_fast, s.groq_model_reasoning, s.judge_model):
        assert model in p.models


def test_cost_math_and_unknown_model() -> None:
    p = Pricing.load(ROOT / "config" / "model_pricing.yaml")
    assert p.cost("openai/gpt-oss-120b", 1_000_000, 1_000_000) == Decimal("0.750000")
    assert p.cost("no/such-model", 10, 10) == Decimal(0)
    assert p.estimate("openai/gpt-oss-20b", 400, 100) > 0


def test_missing_pricing_file(tmp_path: Path) -> None:
    assert Pricing.load(tmp_path / "none.yaml").models == {}


def test_budget_windows_are_clinic_local(session: Session) -> None:
    # 2026-11-30 20:00 UTC is already 2026-12-01 01:30 in IST: a new day and a new month.
    clock = FixedClock(datetime(2026, 11, 30, 20, 0, tzinfo=UTC))
    guard = BudgetGuard(session, clock, llm_test_settings())
    day, month = guard.windows()
    assert day.date().isoformat() == "2026-12-01" and month == day
    assert guard.spent() == (Decimal(0), Decimal(0))
    guard.check(Decimal("1.0"))
    with pytest.raises(BudgetExceeded):
        guard.check(Decimal("1.6"))
