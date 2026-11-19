"""GroqProvider request/response/error mapping with a stubbed `groq.Groq` client."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import groq
import httpx
import pytest

from netracare.llm.providers import GroqProvider
from netracare.llm.types import ChatMessage, LLMRequest, LLMUnavailable, ProviderRejected

REQ = LLMRequest(
    model="openai/gpt-oss-20b",
    messages=[ChatMessage("system", "s"), ChatMessage("user", "u")],
    temperature=0.0,
    max_tokens=100,
    reasoning_effort="low",
    json_schema={"type": "object", "properties": {}},
    schema_name="Demo",
    timeout_s=5,
)


def _completion(content: str = '{"a": 1}', reasoning: str = "secret chain of thought") -> Any:
    message = SimpleNamespace(content=content, reasoning=reasoning)
    return SimpleNamespace(
        choices=[SimpleNamespace(message=message, finish_reason="stop")],
        usage=SimpleNamespace(prompt_tokens=11, completion_tokens=7),
        model="openai/gpt-oss-20b",
    )


class StubClient:
    def __init__(self, outcomes: list[Any]) -> None:
        self.outcomes = outcomes
        self.calls: list[dict[str, Any]] = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        item = self.outcomes.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _http(status: int, headers: dict[str, str] | None = None) -> httpx.Response:
    return httpx.Response(status, headers=headers or {}, request=httpx.Request("POST", "http://x"))


def _status_error(
    cls: type[groq.APIStatusError], status: int, msg: str = "err", **h: str
) -> Exception:
    return cls(msg, response=_http(status, h), body=None)


def test_request_mapping_strict_schema_and_reasoning_off() -> None:
    stub = StubClient([_completion()])
    out = GroqProvider("k", client=stub).complete(REQ)
    call = stub.calls[0]
    assert call["model"] == "openai/gpt-oss-20b"
    assert call["messages"] == [
        {"role": "system", "content": "s"},
        {"role": "user", "content": "u"},
    ]
    assert call["max_completion_tokens"] == 100 and call["timeout"] == 5
    assert call["include_reasoning"] is False and call["reasoning_effort"] == "low"
    rf = call["response_format"]
    assert rf["type"] == "json_schema" and rf["json_schema"]["strict"] is True
    assert rf["json_schema"]["name"] == "Demo"
    assert out.text == '{"a": 1}' and (out.input_tokens, out.output_tokens) == (11, 7)
    assert "secret" not in repr(out)


def test_plain_text_request_has_no_response_format() -> None:
    stub = StubClient([_completion("hi")])
    req = LLMRequest(model="other/model", messages=[ChatMessage("user", "u")])
    GroqProvider("k", client=stub).complete(req)
    assert "response_format" not in stub.calls[0]
    assert "include_reasoning" not in stub.calls[0]


def test_json_schema_fallback_to_json_object() -> None:
    err = _status_error(groq.BadRequestError, 400, "response_format json_schema not supported")
    stub = StubClient([err, _completion()])
    out = GroqProvider("k", client=stub).complete(REQ)
    assert stub.calls[1]["response_format"] == {"type": "json_object"}
    assert out.text == '{"a": 1}'


def test_fallback_failure_is_mapped() -> None:
    err = _status_error(groq.BadRequestError, 400, "json_schema unsupported")
    stub = StubClient([err, _status_error(groq.InternalServerError, 500)])
    with pytest.raises(LLMUnavailable):
        GroqProvider("k", client=stub).complete(REQ)


def test_other_bad_request_is_not_retryable() -> None:
    stub = StubClient([_status_error(groq.BadRequestError, 400, "messages too long")])
    with pytest.raises(ProviderRejected):
        GroqProvider("k", client=stub).complete(REQ)
    assert len(stub.calls) == 1


@pytest.mark.parametrize(
    ("exc", "expected", "retry_after"),
    [
        (_status_error(groq.RateLimitError, 429, **{"retry-after": "3"}), LLMUnavailable, 3.0),
        (_status_error(groq.InternalServerError, 503), LLMUnavailable, None),
        (_status_error(groq.APIStatusError, 502), LLMUnavailable, None),
        (_status_error(groq.AuthenticationError, 401), ProviderRejected, None),
        (groq.APITimeoutError(request=httpx.Request("POST", "http://x")), LLMUnavailable, None),
        (groq.APIConnectionError(request=httpx.Request("POST", "http://x")), LLMUnavailable, None),
    ],
)
def test_error_mapping(
    exc: Exception, expected: type[Exception], retry_after: float | None
) -> None:
    with pytest.raises(expected) as info:
        GroqProvider("k", client=StubClient([exc])).complete(REQ)
    assert getattr(info.value, "retry_after", None) == retry_after


def test_bad_retry_after_header_ignored() -> None:
    exc = _status_error(groq.RateLimitError, 429, **{"retry-after": "soon"})
    with pytest.raises(LLMUnavailable) as info:
        GroqProvider("k", client=StubClient([exc])).complete(REQ)
    assert info.value.retry_after is None
