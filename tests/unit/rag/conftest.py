from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy.orm import Session, sessionmaker

from netracare.config import Settings
from netracare.llm.client import LLMClient
from netracare.llm.pricing import Pricing
from netracare.llm.prompts import PromptRegistry
from netracare.llm.providers import FakeProvider, LLMProvider
from netracare.rag.retriever import ChunkRecord, RetrievalResult, RetrievedChunk
from tests.conftest import ROOT
from tests.helpers import MutableClock

IST = ZoneInfo("Asia/Kolkata")


def rag_llm_settings(**overrides: object) -> Settings:
    base: dict[str, object] = {"groq_api_key": "test-key", "opik_enabled": False}
    base.update(overrides)
    return Settings(_env_file=None, **base)  # type: ignore[call-arg, arg-type]


@pytest.fixture
def make_llm(rag_factory: sessionmaker[Session]) -> Callable[..., LLMClient]:
    registry = PromptRegistry(ROOT / "prompts").load()
    pricing = Pricing.load(ROOT / "config" / "model_pricing.yaml")

    def _make(provider: LLMProvider | None = None, **settings: object) -> LLMClient:
        return LLMClient(
            settings=rag_llm_settings(**settings),
            clock=MutableClock(datetime(2026, 11, 10, 9, 0, tzinfo=IST)),
            provider=provider or FakeProvider(),
            registry=registry,
            pricing=pricing,
            session_factory=rag_factory,
            sleep=lambda _s: None,
        )

    return _make


def record(
    cid: str,
    text: str,
    *,
    slug: str = "doc",
    title: str = "Doc",
    heading: str = "Section",
    doc_type: str = "general_faq",
    effective: date = date(2026, 1, 1),
    current: bool = True,
) -> ChunkRecord:
    return ChunkRecord(
        id=cid,
        slug=slug,
        title=title,
        version=1,
        effective_date=effective,
        doc_type=doc_type,
        heading_path=heading,
        text=text,
        embed_text=f"{title} › {heading}\n\n{text}",
        is_current=current,
        doctor_code=None,
        specialty=None,
        procedure=None,
    )


def result(*records: ChunkRecord, confidence: float = 0.9) -> RetrievalResult:
    return RetrievalResult(
        query="q",
        normalized_query="q",
        chunks=[RetrievedChunk(r, i, 1 / (60 + i)) for i, r in enumerate(records, start=1)],
        confidence=confidence,
        mode="hybrid",
    )
