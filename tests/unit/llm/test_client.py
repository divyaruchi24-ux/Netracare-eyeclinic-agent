from __future__ import annotations

from collections.abc import Callable, Iterator
from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from netracare.db.models import LLMCall
from netracare.db.session import begin_write
from netracare.domain.enums import LLMCallStatus
from netracare.llm.client import LLMClient, make_provider
from netracare.llm.providers import DisabledProvider, FakeProvider, GroqProvider
from netracare.llm.schemas import TriageLLMOutput
from netracare.llm.types import (
    BudgetExceeded,
    InvalidOutput,
    LLMDisabled,
    LLMRequest,
    LLMUnavailable,
    ProviderRejected,
    ProviderResponse,
)
from netracare.observability.tracing import set_backend
from tests.helpers import MutableClock
from tests.llm_helpers import RecordingBackend, resp, triage_json
from tests.unit.llm.conftest import llm_test_settings

MakeClient = Callable[..., LLMClient]


def _rows(factory: sessionmaker[Session]) -> list[LLMCall]:
    with factory() as s:
        return list(s.scalars(select(LLMCall).order_by(LLMCall.at_utc)))


def _triage(client: LLMClient, text: str = "my eye itches", **kw: object) -> object:
    return client.generate("red_flag_classifier", {"text": text}, purpose="triage", **kw)  # type: ignore[arg-type]


@pytest.fixture
def backend() -> Iterator[RecordingBackend]:
    b = RecordingBackend()
    set_backend(b)
    yield b
    set_backend(None)


def test_success_records_call_and_cost(
    make_client: MakeClient, factory: sessionmaker[Session]
) -> None:
    fake = FakeProvider([resp(triage_json(), tin=1000, tout=500, model="openai/gpt-oss-20b")])
    result = make_client(fake).generate(
        "red_flag_classifier", {"text": "itchy"}, purpose="triage", actor_user_id="u1"
    )
    assert isinstance(result.value, TriageLLMOutput)
    # 1000 * 0.075/1M + 500 * 0.30/1M
    assert result.cost_usd == Decimal("0.000225")
    assert result.prompt_id == "red_flag_classifier" and result.prompt_version == 1
    (row,) = _rows(factory)
    assert row.status == LLMCallStatus.OK
    assert (row.input_tokens, row.output_tokens, row.attempts) == (1000, 500, 1)
    assert row.cost_usd == Decimal("0.000225")
    assert row.actor_user_id == "u1" and row.purpose == "triage" and len(row.prompt_hash) == 64
    req = fake.requests[0]
    assert req.model == "openai/gpt-oss-20b" and req.json_schema is not None
    assert req.reasoning_effort == "low" and req.timeout_s == 15.0


def test_reasoning_tier_uses_reasoning_model(make_client: MakeClient) -> None:
    draft = {
        "what_we_checked": [],
        "what_we_found": "ok",
        "next_steps": [],
        "medicines_mentioned": [],
        "follow_up": None,
        "unclear_in_notes": [],
    }
    fake = FakeProvider([resp(draft)])
    make_client(fake).generate("visit_summary", {"notes": "n"}, purpose="visit_summary")
    assert fake.requests[0].model == "openai/gpt-oss-120b"
    assert fake.requests[0].timeout_s == 45.0


def test_retries_on_429_then_succeeds(
    make_client: MakeClient, factory: sessionmaker[Session]
) -> None:
    fake = FakeProvider([LLMUnavailable("429", retry_after=1), resp(triage_json())])
    result = _triage(make_client(fake))
    assert result.attempts == 2  # type: ignore[attr-defined]
    assert _rows(factory)[0].attempts == 2


def test_gives_up_after_max_retries_raises_unavailable(
    make_client: MakeClient, factory: sessionmaker[Session]
) -> None:
    fake = FakeProvider([LLMUnavailable("503")] * 5)
    with pytest.raises(LLMUnavailable):
        _triage(make_client(fake))
    assert fake.calls == 3
    (row,) = _rows(factory)
    assert row.status == LLMCallStatus.ERROR and row.error_code == "llm_unavailable"


def test_timeout_is_retryable(make_client: MakeClient) -> None:
    fake = FakeProvider([LLMUnavailable("APITimeoutError"), resp(triage_json())])
    assert _triage(make_client(fake)).attempts == 2  # type: ignore[attr-defined]


def test_no_retry_on_bad_request(make_client: MakeClient) -> None:
    fake = FakeProvider([ProviderRejected("BadRequestError"), resp(triage_json())])
    with pytest.raises(ProviderRejected):
        _triage(make_client(fake))
    assert fake.calls == 1


def test_unexpected_provider_exception_becomes_unavailable(make_client: MakeClient) -> None:
    fake = FakeProvider([RuntimeError("boom")])
    with pytest.raises(LLMUnavailable):
        _triage(make_client(fake))


def test_budget_daily_blocks_before_provider_call(
    make_client: MakeClient, factory: sessionmaker[Session], llm_clock: MutableClock
) -> None:
    _seed_spend(factory, llm_clock, Decimal("1.50"))
    fake = FakeProvider([resp(triage_json())])
    with pytest.raises(BudgetExceeded) as exc:
        _triage(make_client(fake))
    assert exc.value.scope == "daily"
    assert fake.calls == 0
    assert _rows(factory)[-1].status == LLMCallStatus.BUDGET_BLOCKED


def test_budget_monthly_blocks(
    make_client: MakeClient, factory: sessionmaker[Session], llm_clock: MutableClock
) -> None:
    # Spent earlier this month (not today): only the monthly cap applies.
    _seed_spend(factory, llm_clock, Decimal("20.00"), days_ago=3)
    fake = FakeProvider([resp(triage_json())])
    with pytest.raises(BudgetExceeded) as exc:
        _triage(make_client(fake))
    assert exc.value.scope == "monthly"
    assert fake.calls == 0


def test_last_month_spend_does_not_count(
    make_client: MakeClient, factory: sessionmaker[Session], llm_clock: MutableClock
) -> None:
    _seed_spend(factory, llm_clock, Decimal("50.00"), days_ago=15)  # Oct 26 -> previous month
    fake = FakeProvider([resp(triage_json())])
    _triage(make_client(fake))
    assert fake.calls == 1


def test_invalid_json_repaired_once(
    make_client: MakeClient, factory: sessionmaker[Session]
) -> None:
    fake = FakeProvider([resp("not json at all"), resp(triage_json(level="urgent"))])
    result = _triage(make_client(fake))
    assert result.value.level == "urgent"  # type: ignore[attr-defined]
    assert fake.calls == 2
    assert "Validation errors" in fake.requests[1].messages[1].content
    (row,) = _rows(factory)
    assert row.attempts == 2 and row.status == LLMCallStatus.OK


def test_schema_violation_repaired(make_client: MakeClient) -> None:
    bad = triage_json() | {"confidence": 7}
    fake = FakeProvider([resp(bad), resp(triage_json(confidence=0.7))])
    assert _triage(make_client(fake)).value.confidence == 0.7  # type: ignore[attr-defined]


def test_fenced_json_is_accepted(make_client: MakeClient) -> None:
    import json

    fake = FakeProvider([resp("```json\n" + json.dumps(triage_json()) + "\n```")])
    assert _triage(make_client(fake)).attempts == 1  # type: ignore[attr-defined]


def test_invalid_after_repair_raises(
    make_client: MakeClient, factory: sessionmaker[Session]
) -> None:
    fake = FakeProvider([resp("nope"), resp("still nope")])
    with pytest.raises(InvalidOutput):
        _triage(make_client(fake))
    (row,) = _rows(factory)
    assert row.status == LLMCallStatus.INVALID_OUTPUT
    assert row.input_tokens == 200  # both calls billed


def test_reasoning_text_dropped(make_client: MakeClient) -> None:
    """Only `content` reaches the caller; providers never surface the reasoning field."""
    fake = FakeProvider(lambda _r: ProviderResponse(text="plain answer", input_tokens=1))
    result = make_client(fake).generate(
        "json_repair", {"schema": "{}", "errors": "", "output": "x"}, purpose="t"
    )
    assert result.value == "plain answer"


def test_pii_redacted_in_prompt_sent(make_client: MakeClient) -> None:
    fake = FakeProvider([resp(triage_json())])
    _triage(
        make_client(fake),
        "Asha Verma here, call 98450 12345 or asha.v@example.com, my eye hurts",
        redact_names=["Asha Verma"],
    )
    sent = fake.sent_text()
    assert "Asha" not in sent and "Verma" not in sent
    assert "98450" not in sent and "asha.v@example.com" not in sent
    assert "the patient" in sent and "[PHONE]" in sent and "[EMAIL]" in sent


def test_llm_call_row_has_no_prompt_text(
    make_client: MakeClient, factory: sessionmaker[Session], backend: RecordingBackend
) -> None:
    secret = "zebra-secret-symptom-text"
    fake = FakeProvider([resp(triage_json())])
    _triage(make_client(fake), secret)
    (row,) = _rows(factory)
    values = [str(getattr(row, c.key)) for c in LLMCall.__table__.columns]
    assert not any(secret in v for v in values)
    (span,) = backend.spans
    assert span["type"] == "llm" and span["name"] == "llm.triage"
    assert secret not in str(span)
    assert span["usage"]["provider"] == "fake" and span["usage"]["prompt_tokens"] == 100
    assert span["metadata"]["prompt"] == "red_flag_classifier@v1"


def test_failed_span_records_error(make_client: MakeClient, backend: RecordingBackend) -> None:
    with pytest.raises(LLMUnavailable):
        _triage(make_client(FakeProvider([])))
    assert backend.spans[0]["output"]["code"] == "llm_unavailable"


def test_failed_call_row_survives_rollback(
    make_client: MakeClient, factory: sessionmaker[Session]
) -> None:
    request_session = factory()
    with pytest.raises(LLMUnavailable):
        _triage(make_client(FakeProvider([])))
    request_session.rollback()  # the request fails and rolls back...
    request_session.close()
    assert len(_rows(factory)) == 1  # ...but the usage row was committed on its own


def test_disabled_raises_llm_disabled(
    make_client: MakeClient, factory: sessionmaker[Session]
) -> None:
    client = make_client(FakeProvider(), settings=llm_test_settings(llm_enabled=False))
    assert not client.available
    with pytest.raises(LLMDisabled):
        _triage(client)
    with pytest.raises(LLMDisabled):
        _triage(make_client(DisabledProvider()))
    assert _rows(factory) == []  # nothing attempted, nothing billed


def test_record_failure_never_breaks_the_call(
    make_client: MakeClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(**_kw: object) -> LLMCall:
        raise RuntimeError("db down")

    monkeypatch.setattr("netracare.llm.client.LLMCall", broken)
    result = _triage(make_client(FakeProvider([resp(triage_json())])))
    assert result.call_id  # type: ignore[attr-defined]


def test_make_provider() -> None:
    assert isinstance(make_provider(llm_test_settings(groq_api_key=None)), DisabledProvider)
    assert isinstance(make_provider(llm_test_settings(llm_enabled=False)), DisabledProvider)
    assert isinstance(make_provider(llm_test_settings()), GroqProvider)


def test_fake_provider_script_and_callable() -> None:
    req = LLMRequest(model="m", messages=[])
    fake = FakeProvider(lambda r: ProviderResponse(text=r.model))
    assert fake.complete(req).text == "m"
    with pytest.raises(LLMUnavailable):
        FakeProvider().complete(req)


def _seed_spend(
    factory: sessionmaker[Session], clock: MutableClock, cost: Decimal, days_ago: int = 0
) -> None:
    with factory() as s:
        begin_write(s)
        s.add(
            LLMCall(
                at_utc=clock.now_utc() - timedelta(days=days_ago),
                purpose="seed",
                prompt_id="x",
                prompt_version=1,
                prompt_hash="0" * 64,
                model="openai/gpt-oss-20b",
                cost_usd=cost,
                status=LLMCallStatus.OK,
            )
        )
        s.commit()
