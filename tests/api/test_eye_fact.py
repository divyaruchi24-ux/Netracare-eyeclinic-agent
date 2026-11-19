"""GET /eye-fact: deterministic daily topic, generated once, validated, static fallback."""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session

from netracare.db.models import EyeFact, LLMCall
from netracare.db.session import make_session_factory
from netracare.guardrails.scripts import DISCLAIMERS
from netracare.llm.providers import FakeProvider
from netracare.llm.types import BudgetExceeded, LLMUnavailable
from netracare.services import eye_fact_service
from netracare.services.eye_fact_service import EyeFactService, load_topics, topic_for
from tests.conftest import ROOT
from tests.fixtures_api import TODAY, fresh
from tests.helpers import MutableClock
from tests.llm_helpers import resp

UseLLM = Callable[..., FakeProvider]
TOPICS, STATIC = load_topics(str(ROOT / "prompts"))
TODAY_TOPIC = topic_for(TODAY, TOPICS)


def _fact(text: str, topic: str = TODAY_TOPIC.id) -> object:
    return resp({"fact": text, "topic": topic})


def _good_text() -> str:
    return f"A fun fact: the {TODAY_TOPIC.keywords[0]} is part of how the eye works every day."


def test_topic_is_deterministic_and_varies() -> None:
    assert topic_for(TODAY, TOPICS) == topic_for(date(2026, 11, 10), TOPICS)
    seen = {topic_for(TODAY + timedelta(days=i), TOPICS).id for i in range(60)}
    assert len(seen) >= 10


def test_llm_fact_generated_once_per_day(client: TestClient, use_llm: UseLLM, db: Session) -> None:
    fake = use_llm([_fact(_good_text())])
    first = client.get("/eye-fact").json()
    second = client.get("/eye-fact").json()
    assert first == second
    assert first["source"] == "llm" and first["text"] == _good_text()
    assert first["topic"] == TODAY_TOPIC.id and first["day"] == TODAY.isoformat()
    assert (
        first["disclaimer"]
        == DISCLAIMERS["eye_fact"]
        == "General eye-health fact. Not medical advice."
    )
    assert fake.calls == 1
    assert fresh(db).scalar(select(func.count()).select_from(LLMCall)) == 1


def test_disabled_llm_uses_static_fact(client: TestClient) -> None:
    body = client.get("/eye-fact").json()
    assert body["source"] == "static" and body["text"] == STATIC[TODAY_TOPIC.id]


@pytest.mark.parametrize(
    "bad",
    [
        "Use artificial tears daily to help your {kw}.",
        "You should take vitamin A supplements for your {kw}.",
        "Your {kw} is affected by glaucoma, a disease that can cause blindness.",
        "About 20% of the {kw} is water, per www.eyes.example.",
        "Carrots are orange and crunchy.",
    ],
)
def test_unsafe_llm_output_replaced_by_static(
    client: TestClient, use_llm: UseLLM, db: Session, bad: str
) -> None:
    use_llm([_fact(bad.format(kw=TODAY_TOPIC.keywords[0]))])
    body = client.get("/eye-fact").json()
    assert body["source"] == "static" and body["text"] == STATIC[TODAY_TOPIC.id]
    row = fresh(db).get(EyeFact, TODAY)
    assert row is not None and row.rejected_reason


@pytest.mark.parametrize("error", [LLMUnavailable("down"), BudgetExceeded("daily")])
def test_llm_error_or_budget_gives_static(
    client: TestClient, use_llm: UseLLM, error: Exception
) -> None:
    use_llm([error] * 3)
    body = client.get("/eye-fact").json()
    assert body["source"] == "static"


def test_concurrent_first_insert_is_safe(
    db: Session,
    api_engine: Engine,
    api_db_url: str,
    clock: MutableClock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.fixtures_api import make_settings

    svc = EyeFactService(db, clock, make_settings(api_db_url), None)
    real = svc._generate

    def racing(topic: eye_fact_service.Topic) -> tuple[str, str, int | None, str | None]:
        # Another request inserts today's row while we are "generating".
        with make_session_factory(api_engine)() as other:
            other.add(EyeFact(day=TODAY, topic=topic.id, text="winner", source="static"))
            other.commit()
        return real(topic)

    monkeypatch.setattr(svc, "_generate", racing)
    view = svc.for_day(TODAY)
    assert view.text == "winner"


def test_cli_job_pregenerates(
    api_db_url: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from netracare.llm import jobs
    from tests.fixtures_api import make_settings

    settings = make_settings(api_db_url)
    monkeypatch.setattr(jobs, "get_settings", lambda: settings)
    assert jobs.main(["eye-fact", "--date", "2026-12-01"]) == 0
    last = capsys.readouterr().out.strip().splitlines()[-1]
    assert last.startswith("2026-12-01 [static]")
