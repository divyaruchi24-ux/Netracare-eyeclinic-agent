"""Daily and monthly LLM spend caps, computed from `llm_calls` in clinic-local time."""

from __future__ import annotations

from datetime import datetime, time, timedelta
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from netracare.config import Settings
from netracare.db.models import LLMCall
from netracare.domain.clock import Clock
from netracare.llm.types import BudgetExceeded


def _local_day_start(now_local: datetime) -> datetime:
    return datetime.combine(now_local.date(), time.min, tzinfo=now_local.tzinfo)


def spend_since(session: Session, since_utc: datetime) -> Decimal:
    total = session.scalar(
        select(func.coalesce(func.sum(LLMCall.cost_usd), 0)).where(LLMCall.at_utc >= since_utc)
    )
    return Decimal(str(total or 0))


class BudgetGuard:
    def __init__(self, session: Session, clock: Clock, settings: Settings) -> None:
        self.session = session
        self.clock = clock
        self.settings = settings

    def windows(self) -> tuple[datetime, datetime]:
        """(start of today, start of this month) in clinic-local time, as aware datetimes."""
        now_local = self.clock.now_utc().astimezone(self.settings.tz)
        day = _local_day_start(now_local)
        month = day - timedelta(days=now_local.day - 1)
        return day, month

    def spent(self) -> tuple[Decimal, Decimal]:
        day, month = self.windows()
        return spend_since(self.session, day), spend_since(self.session, month)

    def check(self, estimated_usd: Decimal = Decimal(0)) -> None:
        today, month = self.spent()
        if today + estimated_usd > Decimal(str(self.settings.daily_llm_budget_usd)):
            raise BudgetExceeded("daily")
        if month + estimated_usd > Decimal(str(self.settings.monthly_llm_budget_usd)):
            raise BudgetExceeded("monthly")
