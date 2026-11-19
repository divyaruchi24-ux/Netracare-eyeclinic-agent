"""`LLMClient`: the only way features talk to an LLM.

Per call: disabled check -> render prompt -> redact PII -> budget check -> provider call with
timeout and retries (exponential backoff + jitter, honouring retry-after; only for retryable
errors) -> parse + validate (one repair round-trip) -> cost -> `llm_calls` row -> Opik span.

The `llm_calls` row is written in its own short transaction (own session), so it survives a
rollback of the request. Callers must not hold a DB write lock while calling `generate`
(see `netracare.db.session.outside_transaction`).
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError
from sqlalchemy.orm import Session, sessionmaker
from tenacity import (
    RetryCallState,
    Retrying,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential_jitter,
)

from netracare.config import Settings
from netracare.db.base import new_id
from netracare.db.models import LLMCall
from netracare.db.session import session_scope
from netracare.domain.clock import Clock
from netracare.domain.enums import LLMCallStatus
from netracare.llm.budget import BudgetGuard
from netracare.llm.pricing import Pricing
from netracare.llm.prompts import LoadedPrompt, PromptRegistry, render
from netracare.llm.providers import DisabledProvider, FakeProvider, GroqProvider, LLMProvider
from netracare.llm.schemas import SCHEMAS, strict_json_schema
from netracare.llm.types import (
    BudgetExceeded,
    ChatMessage,
    InvalidOutput,
    LLMDisabled,
    LLMError,
    LLMRequest,
    LLMResult,
    LLMUnavailable,
    ProviderResponse,
)
from netracare.log import get_logger, redact_all, request_id_var
from netracare.observability.tracing import llm_span

M = TypeVar("M", bound=BaseModel)
log = get_logger(__name__)

_MAX_RETRY_AFTER_S = 20.0


@dataclass
class _Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    attempts: int = 0
    model: str = ""

    def add(self, resp: ProviderResponse) -> None:
        self.input_tokens += resp.input_tokens
        self.output_tokens += resp.output_tokens
        self.model = resp.model or self.model


def _wait(base: Callable[[RetryCallState], float]) -> Callable[[RetryCallState], float]:
    """Exponential backoff with jitter, but at least the server's retry-after (capped)."""

    def wait(state: RetryCallState) -> float:
        delay = base(state)
        exc = state.outcome.exception() if state.outcome else None
        hint = getattr(exc, "retry_after", None)
        if hint:
            delay = max(delay, min(float(hint), _MAX_RETRY_AFTER_S))
        return delay

    return wait


def _parse_json(text: str) -> Any:
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        text = text[text.find("\n") + 1 :] if "\n" in text else text
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end < start:
        raise ValueError("no JSON object in output")
    return json.loads(text[start : end + 1])


class LLMClient:
    def __init__(
        self,
        *,
        settings: Settings,
        clock: Clock,
        provider: LLMProvider,
        registry: PromptRegistry,
        pricing: Pricing,
        session_factory: sessionmaker[Session],
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.settings = settings
        self.clock = clock
        self.provider = provider
        self.registry = registry
        self.pricing = pricing
        self.session_factory = session_factory
        self._sleep = sleep

    @property
    def available(self) -> bool:
        return self.settings.llm_enabled and not isinstance(self.provider, DisabledProvider)

    def model_for(self, prompt: LoadedPrompt) -> str:
        s = self.settings
        return s.groq_model_fast if prompt.spec.tier == "fast" else s.groq_model_reasoning

    def _timeout_for(self, prompt: LoadedPrompt) -> float:
        s = self.settings
        return s.llm_timeout_fast_s if prompt.spec.tier == "fast" else s.llm_timeout_reasoning_s

    # ------------------------------------------------------------------ public API

    def generate(
        self,
        prompt_id: str,
        variables: Mapping[str, Any],
        *,
        purpose: str,
        schema: type[M] | None = None,
        actor_user_id: str | None = None,
        redact_names: Iterable[str] = (),
        version: int | None = None,
    ) -> LLMResult[Any]:
        """Run one prompt. Returns `LLMResult.value` as a validated `schema` instance (or the
        prompt's registered `output_schema`), else the raw text. Raises `LLMError`s."""
        prompt = self.registry.get(prompt_id, version)
        model = self.model_for(prompt)
        if schema is None and prompt.spec.output_schema:
            schema = SCHEMAS[prompt.spec.output_schema]  # type: ignore[assignment]
        names = list(redact_names)
        if not self.available:  # not recorded: no call was attempted
            raise LLMDisabled("LLM features are disabled or GROQ_API_KEY is not set")

        safe_vars = {k: redact_all(str(v), names) for k, v in variables.items()}
        messages = render(prompt, safe_vars)
        input_chars = sum(len(m.content) for m in messages)
        usage = _Usage(model=model)
        started = time.perf_counter()
        span_input = {"input_chars": input_chars, "schema": schema.__name__ if schema else None}
        with llm_span(
            f"llm.{purpose}", model=model, prompt=prompt.ref, purpose=purpose, input=span_input
        ) as span:
            try:
                self._check_budget(model, input_chars, prompt.spec.max_tokens)
                value = self._run(prompt, model, messages, schema, usage, names)
            except LLMError as exc:
                latency = int((time.perf_counter() - started) * 1000)
                status = (
                    LLMCallStatus.BUDGET_BLOCKED
                    if isinstance(exc, BudgetExceeded)
                    else LLMCallStatus.INVALID_OUTPUT
                    if isinstance(exc, InvalidOutput)
                    else LLMCallStatus.ERROR
                )
                cost = self.pricing.cost(
                    usage.model or model, usage.input_tokens, usage.output_tokens
                )
                self._record(
                    prompt,
                    usage.model or model,
                    purpose,
                    status,
                    usage,
                    latency,
                    actor_user_id,
                    exc.code,
                    cost,
                )
                span.set_usage(
                    model=usage.model or model,
                    provider=self.provider.name,
                    input_tokens=usage.input_tokens,
                    output_tokens=usage.output_tokens,
                    cost_usd=float(cost),
                )
                log.warning(
                    "llm.call_failed",
                    prompt=prompt.ref,
                    purpose=purpose,
                    error=exc.code,
                    attempts=usage.attempts,
                )
                raise
            latency = int((time.perf_counter() - started) * 1000)
            used_model = usage.model or model
            cost = self.pricing.cost(used_model, usage.input_tokens, usage.output_tokens)
            call_id = self._record(
                prompt,
                used_model,
                purpose,
                LLMCallStatus.OK,
                usage,
                latency,
                actor_user_id,
                None,
                cost,
            )
            span.set_usage(
                model=used_model,
                provider=self.provider.name,
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                cost_usd=float(cost),
            )
            span.set_output(
                {"ok": True, "attempts": usage.attempts, "output_type": type(value).__name__}
            )
        log.info(
            "llm.call",
            prompt=prompt.ref,
            purpose=purpose,
            model=used_model,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cost_usd=str(cost),
            latency_ms=latency,
            attempts=usage.attempts,
        )
        return LLMResult(
            value=value,
            call_id=call_id,
            model=used_model,
            cost_usd=cost,
            latency_ms=latency,
            prompt_id=prompt.spec.id,
            prompt_version=prompt.spec.version,
            attempts=usage.attempts,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
        )

    # ------------------------------------------------------------------ internals

    def _check_budget(self, model: str, input_chars: int, max_tokens: int) -> None:
        estimate = self.pricing.estimate(model, input_chars, max_tokens)
        with session_scope(self.session_factory) as s:
            BudgetGuard(s, self.clock, self.settings).check(estimate)

    def _complete(self, request: LLMRequest, usage: _Usage) -> ProviderResponse:
        retrying = Retrying(
            stop=stop_after_attempt(self.settings.llm_max_retries),
            wait=_wait(wait_exponential_jitter(multiplier=0.5, max=8.0, jitter=0.5)),
            retry=retry_if_exception(lambda e: isinstance(e, LLMError) and e.retryable),
            sleep=self._sleep,
            reraise=True,
        )
        try:
            for attempt in retrying:
                with attempt:
                    usage.attempts += 1
                    resp = self.provider.complete(request)
                    usage.add(resp)
                    return resp
        except LLMUnavailable:
            raise
        except LLMError:
            raise
        except Exception as exc:  # provider bug / unexpected exception: never a 500 upstream
            raise LLMUnavailable(type(exc).__name__) from exc
        raise LLMUnavailable("retries exhausted")  # pragma: no cover - reraise=True

    def _run(
        self,
        prompt: LoadedPrompt,
        model: str,
        messages: list[ChatMessage],
        schema: type[BaseModel] | None,
        usage: _Usage,
        names: list[str],
    ) -> Any:
        json_schema = strict_json_schema(schema) if schema else None
        request = LLMRequest(
            model=model,
            messages=messages,
            temperature=prompt.spec.temperature,
            max_tokens=prompt.spec.max_tokens,
            reasoning_effort=prompt.spec.reasoning_effort,
            json_schema=json_schema,
            schema_name=schema.__name__ if schema else None,
            timeout_s=self._timeout_for(prompt),
        )
        resp = self._complete(request, usage)
        if schema is None:
            return resp.text
        try:
            return schema.model_validate(_parse_json(resp.text))
        except (ValueError, ValidationError) as exc:
            errors = _error_summary(exc)
        log.info("llm.repair", prompt=prompt.ref, errors=errors[:200])
        repaired = self._repair(schema, json_schema or {}, errors, resp.text, usage, names)
        try:
            return schema.model_validate(_parse_json(repaired))
        except (ValueError, ValidationError) as exc:
            raise InvalidOutput(f"{prompt.ref}: output invalid after repair") from exc

    def _repair(
        self,
        schema: type[BaseModel],
        json_schema: dict[str, Any],
        errors: str,
        output: str,
        usage: _Usage,
        names: list[str],
    ) -> str:
        repair = self.registry.get("json_repair")
        model = self.model_for(repair)
        messages = render(
            repair,
            {
                "schema": json.dumps(json_schema),
                "errors": errors,
                "output": redact_all(output[:6000], names),
            },
        )
        request = LLMRequest(
            model=model,
            messages=messages,
            temperature=repair.spec.temperature,
            max_tokens=repair.spec.max_tokens,
            reasoning_effort=repair.spec.reasoning_effort,
            json_schema=json_schema,
            schema_name=schema.__name__,
            timeout_s=self._timeout_for(repair),
        )
        return self._complete(request, usage).text

    def _record(
        self,
        prompt: LoadedPrompt,
        model: str,
        purpose: str,
        status: LLMCallStatus,
        usage: _Usage,
        latency_ms: int,
        actor_user_id: str | None,
        error_code: str | None,
        cost: Decimal = Decimal(0),
    ) -> str:
        """Write the usage row in its own transaction. Never raises."""
        call_id = new_id()
        try:
            with session_scope(self.session_factory, write=True) as s:
                s.add(
                    LLMCall(
                        id=call_id,
                        at_utc=self.clock.now_utc(),
                        request_id=request_id_var.get(),
                        purpose=purpose,
                        prompt_id=prompt.spec.id,
                        prompt_version=prompt.spec.version,
                        prompt_hash=prompt.content_hash,
                        model=model,
                        input_tokens=usage.input_tokens,
                        output_tokens=usage.output_tokens,
                        cost_usd=cost,
                        latency_ms=latency_ms,
                        attempts=usage.attempts,
                        status=status,
                        error_code=error_code,
                        actor_user_id=actor_user_id,
                    )
                )
        except Exception as exc:
            log.error("llm.record_failed", error=type(exc).__name__)
        return call_id


def _error_summary(exc: Exception) -> str:
    if isinstance(exc, ValidationError):
        return "; ".join(
            f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()[:10]
        )
    return str(exc)[:300]


def make_provider(settings: Settings) -> LLMProvider:
    """Groq when enabled and keyed; otherwise a provider that fails fast (features fall back)."""
    key = settings.groq_api_key.get_secret_value() if settings.groq_api_key else ""
    if not settings.llm_enabled or not key:
        return DisabledProvider()
    return GroqProvider(key)


__all__ = ["FakeProvider", "LLMClient", "make_provider"]
