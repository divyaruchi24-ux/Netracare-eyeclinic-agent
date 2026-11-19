"""LLM providers: the `LLMProvider` protocol, `GroqProvider`, and the scripted `FakeProvider`."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Protocol

from netracare.llm.types import (
    ChatMessage,
    LLMDisabled,
    LLMError,
    LLMRequest,
    LLMUnavailable,
    ProviderRejected,
    ProviderResponse,
)
from netracare.log import get_logger

log = get_logger(__name__)


class LLMProvider(Protocol):
    name: str

    def complete(self, request: LLMRequest) -> ProviderResponse: ...


def _retry_after(exc: Any) -> float | None:
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None)
    if not headers:
        return None
    try:
        value = headers.get("retry-after")
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _is_schema_rejection(exc: Exception) -> bool:
    text = str(exc).lower()
    return "json_schema" in text or "response_format" in text or "schema" in text


class GroqProvider:
    """Groq chat completions. gpt-oss reasoning text is never requested or kept."""

    name = "groq"

    def __init__(self, api_key: str, *, base_url: str | None = None, client: Any = None) -> None:
        if client is None:  # pragma: no cover - exercised by the live smoke test
            import groq

            client = groq.Groq(api_key=api_key, base_url=base_url, max_retries=0)
        self._client = client

    def _messages(self, messages: list[ChatMessage]) -> list[dict[str, str]]:
        return [{"role": m.role, "content": m.content} for m in messages]

    def _kwargs(self, request: LLMRequest, *, mode: str) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "model": request.model,
            "messages": self._messages(request.messages),
            "temperature": request.temperature,
            "max_completion_tokens": request.max_tokens,
            "timeout": request.timeout_s,
        }
        if request.model.startswith("openai/gpt-oss"):
            kwargs["include_reasoning"] = False
            if request.reasoning_effort:
                kwargs["reasoning_effort"] = request.reasoning_effort
        if request.json_schema is not None:
            if mode == "json_schema":
                kwargs["response_format"] = {
                    "type": "json_schema",
                    "json_schema": {
                        "name": request.schema_name or "output",
                        "schema": request.json_schema,
                        "strict": True,
                    },
                }
            else:
                kwargs["response_format"] = {"type": "json_object"}
        return kwargs

    def complete(self, request: LLMRequest) -> ProviderResponse:
        import groq

        try:
            return self._call(request, mode="json_schema")
        except groq.BadRequestError as exc:
            if request.json_schema is not None and _is_schema_rejection(exc):
                log.warning("llm.json_schema_fallback", model=request.model)
                try:
                    return self._call(request, mode="json_object")
                except groq.GroqError as exc2:
                    raise _map_error(exc2) from exc2
            raise _map_error(exc) from exc
        except groq.GroqError as exc:
            raise _map_error(exc) from exc

    def _call(self, request: LLMRequest, *, mode: str) -> ProviderResponse:
        resp = self._client.chat.completions.create(**self._kwargs(request, mode=mode))
        choice = resp.choices[0]
        usage = resp.usage
        # Only `content` is read: the `reasoning` field (if any) is dropped here, never stored.
        return ProviderResponse(
            text=choice.message.content or "",
            input_tokens=getattr(usage, "prompt_tokens", 0) or 0,
            output_tokens=getattr(usage, "completion_tokens", 0) or 0,
            model=resp.model or request.model,
            finish_reason=choice.finish_reason,
        )

    def list_models(self) -> list[str]:  # pragma: no cover - live only
        return [m.id for m in self._client.models.list().data]


def _map_error(exc: Exception) -> LLMError:
    import groq

    name = type(exc).__name__
    if isinstance(exc, groq.RateLimitError):
        return LLMUnavailable(f"rate limited ({name})", retry_after=_retry_after(exc))
    if isinstance(exc, groq.APITimeoutError | groq.APIConnectionError):
        return LLMUnavailable(name)
    if isinstance(exc, groq.InternalServerError):
        return LLMUnavailable(name, retry_after=_retry_after(exc))
    if isinstance(exc, groq.APIStatusError) and getattr(exc, "status_code", 0) >= 500:
        return LLMUnavailable(name)
    return ProviderRejected(name)


class DisabledProvider:
    """Used when LLM features are off or no API key is configured: every call fails fast."""

    name = "disabled"

    def complete(self, request: LLMRequest) -> ProviderResponse:
        raise LLMDisabled("LLM features are disabled or GROQ_API_KEY is not set")


Script = list[ProviderResponse | Exception] | Callable[[LLMRequest], ProviderResponse]


class FakeProvider:
    """Deterministic provider for tests: replays a script (or calls a function) and records
    every request it receives."""

    name = "fake"

    def __init__(self, script: Script | None = None) -> None:
        self._script = script if script is not None else []
        self.requests: list[LLMRequest] = []

    def complete(self, request: LLMRequest) -> ProviderResponse:
        self.requests.append(request)
        if callable(self._script):
            return self._script(request)
        if not self._script:
            raise LLMUnavailable("fake provider script exhausted")
        item = self._script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    @property
    def calls(self) -> int:
        return len(self.requests)

    def sent_text(self) -> str:
        """Everything sent to the 'model' so far (for PII assertions)."""
        return "\n".join(m.content for r in self.requests for m in r.messages)
