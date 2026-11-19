"""Public daily eye-fact banner."""

from __future__ import annotations

from datetime import date

from fastapi import APIRouter

from netracare.api.deps import ClockDep, DbSession, LLMDep, SettingsDep
from netracare.api.schemas.common import Schema
from netracare.services.eye_fact_service import EyeFactService

router = APIRouter(prefix="/eye-fact", tags=["eye fact"])


class EyeFactOut(Schema):
    day: date
    topic: str
    text: str
    source: str
    disclaimer: str


@router.get("", response_model=EyeFactOut)
def eye_fact(db: DbSession, clock: ClockDep, settings: SettingsDep, llm: LLMDep) -> EyeFactOut:
    view = EyeFactService(db, clock, settings, llm).today()
    return EyeFactOut(
        day=view.day,
        topic=view.topic,
        text=view.text,
        source=view.source,
        disclaimer=view.disclaimer,
    )
