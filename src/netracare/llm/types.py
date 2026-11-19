"""Value types and errors shared by the LLM layer."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Generic, Literal, TypeVar

T = TypeVar("T")

Role = Literal["system", "user", "assistant"]


@dataclass(frozen=True)
class ChatMessage:
    role: Role
    content: str


@dataclass(frozen=True)
class LLMRequest:
    model: str
    messages: list[ChatMessage]
    temperature: float = 0.0
    max_tokens: int = 512
    reasoning_effort: str | None = None
    json_schema: dict[str, Any] | None = None
    schema_name: str | None = None
    timeout_s: float = 15.0


@dataclass(frozen=True)
class ProviderResponse:
    text: str
    input_tokens: int = 0
    output_tokens: int = 0
    model: str = ""
    finish_reason: str | None = None


@dataclass(frozen=True)
class LLMResult(Generic[T]):
    value: T
    call_id: str
    model: str
    cost_usd: Decimal
    latency_ms: int
    prompt_id: str
    prompt_version: int
    attempts: int
    input_tokens: int = 0
    output_tokens: int = 0
    meta: dict[str, Any] = field(default_factory=dict)


class LLMError(Exception):
    """Base for every LLM failure. Features catch this and degrade gracefully."""

    code = "llm_error"
    retryable = False

    def __init__(self, message: str = "", *, retry_after: float | None = None) -> None:
        super().__init__(message or self.code)
        self.retry_after = retry_after


class LLMUnavailable(LLMError):
    """Timeout / connection / 429 / 5xx (retryable), or retries exhausted."""

    code = "llm_unavailable"
    retryable = True


class ProviderRejected(LLMError):
    """A non-retryable 4xx from the provider (bad request, auth, unknown model)."""

    code = "provider_rejected"


class BudgetExceeded(LLMError):
    code = "budget_exceeded"

    def __init__(self, scope: str) -> None:
        super().__init__(f"{scope} LLM budget exhausted")
        self.scope = scope


class InvalidOutput(LLMError):
    code = "invalid_output"


class LLMDisabled(LLMError):
    code = "llm_disabled"
