"""LLM spend and usage reporting from `llm_calls` (no prompt or completion content)."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from netracare.config import Settings
from netracare.db.models import LLMCall
from netracare.domain.clock import Clock
from netracare.llm.budget import BudgetGuard


@dataclass
class UsageReport:
    days: int
    total_cost_usd: Decimal = Decimal(0)
    total_calls: int = 0
    by_day: dict[date, Decimal] = field(default_factory=dict)
    by_model: dict[str, dict[str, object]] = field(default_factory=dict)
    by_purpose: dict[str, dict[str, object]] = field(default_factory=dict)
    by_status: dict[str, int] = field(default_factory=dict)
    today_usd: Decimal = Decimal(0)
    month_to_date_usd: Decimal = Decimal(0)
    daily_budget_usd: Decimal = Decimal(0)
    monthly_budget_usd: Decimal = Decimal(0)
    recent: list[LLMCall] = field(default_factory=list)


def usage_report(session: Session, clock: Clock, settings: Settings, days: int = 30) -> UsageReport:
    tz = settings.tz
    guard = BudgetGuard(session, clock, settings)
    day_start, _ = guard.windows()
    since = day_start - timedelta(days=days - 1)
    rows = list(
        session.scalars(
            select(LLMCall).where(LLMCall.at_utc >= since).order_by(LLMCall.at_utc.desc())
        )
    )
    report = UsageReport(days=days)
    by_day: dict[date, Decimal] = defaultdict(Decimal)
    models: dict[str, dict[str, object]] = {}
    purposes: dict[str, dict[str, object]] = {}
    statuses: dict[str, int] = defaultdict(int)
    for r in rows:
        cost = Decimal(r.cost_usd or 0)
        report.total_cost_usd += cost
        by_day[r.at_utc.astimezone(tz).date()] += cost
        statuses[r.status.value] += 1
        for bucket, key in ((models, r.model), (purposes, r.purpose)):
            b = bucket.setdefault(
                key, {"calls": 0, "cost_usd": Decimal(0), "input_tokens": 0, "output_tokens": 0}
            )
            b["calls"] = int(b["calls"]) + 1  # type: ignore[call-overload]
            b["cost_usd"] = Decimal(b["cost_usd"]) + cost  # type: ignore[arg-type]
            b["input_tokens"] = int(b["input_tokens"]) + r.input_tokens  # type: ignore[call-overload]
            b["output_tokens"] = int(b["output_tokens"]) + r.output_tokens  # type: ignore[call-overload]
    report.total_calls = len(rows)
    report.by_day = dict(sorted(by_day.items()))
    report.by_model = models
    report.by_purpose = purposes
    report.by_status = dict(statuses)
    report.today_usd, report.month_to_date_usd = guard.spent()
    report.daily_budget_usd = Decimal(str(settings.daily_llm_budget_usd))
    report.monthly_budget_usd = Decimal(str(settings.monthly_llm_budget_usd))
    report.recent = rows[:50]
    return report
