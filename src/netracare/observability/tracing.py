"""PII-safe tracing to Opik.

* Disabled unless `OPIK_ENABLED=true` **and** `OPIK_API_KEY` is set; when disabled, `traced`
  calls the wrapped function directly (the `opik` package is not even imported).
* Payloads are built by explicit `inputs`/`outputs` summarisers (IDs, statuses, codes) and
  then passed through `safe_payload`, which drops PII-named keys and masks emails/phones.
* Tracing can never break the traced operation: backend errors are logged and swallowed.
"""

from __future__ import annotations

import functools
from collections.abc import Callable, Iterator, Mapping
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from datetime import date, datetime
from enum import Enum
from typing import Any, ParamSpec, Protocol, TypeVar

import structlog

from netracare.config import Settings
from netracare.log import get_logger, redact, request_id_var
from netracare.observability.opik_config import apply_opik_env

P = ParamSpec("P")
R = TypeVar("R")

log = get_logger(__name__)

PII_KEYS: frozenset[str] = frozenset(
    {
        "full_name",
        "name",
        "patient_name",
        "phone",
        "email",
        "dob",
        "date_of_birth",
        "password",
        "token",
        "access_token",
        "refresh_token",
        "address",
        "reason",
        "reason_text",
        "note",
    }
)
_MAX_DEPTH = 6


def safe_payload(obj: Any, _depth: int = 0) -> Any:
    """Recursively drop PII-named keys and mask emails/phone numbers in strings."""
    if _depth > _MAX_DEPTH:
        return "[TRUNCATED]"
    if isinstance(obj, Mapping):
        return {
            str(k): safe_payload(v, _depth + 1)
            for k, v in obj.items()
            if str(k).lower() not in PII_KEYS
        }
    if isinstance(obj, list | tuple | set | frozenset):
        return [safe_payload(v, _depth + 1) for v in obj]
    if isinstance(obj, Enum):
        return obj.value
    if isinstance(obj, datetime | date):
        return obj.isoformat()
    if isinstance(obj, str):
        return redact(obj)
    if obj is None or isinstance(obj, bool | int | float):
        return obj
    return redact(str(obj))


class SpanHandle(Protocol):
    def set_output(self, output: dict[str, Any]) -> None: ...


class TraceBackend(Protocol):
    def span(
        self, name: str, *, tags: list[str], input: dict[str, Any], metadata: dict[str, Any]
    ) -> AbstractContextManager[SpanHandle]: ...

    def llm_span(
        self, name: str, *, tags: list[str], input: dict[str, Any], metadata: dict[str, Any]
    ) -> AbstractContextManager[LLMSpanHandle]: ...

    def flush(self) -> None: ...


class LLMSpanHandle(SpanHandle, Protocol):
    def set_usage(
        self, *, model: str, provider: str, usage: dict[str, int], total_cost: float
    ) -> None: ...


class _OpikHandle:
    def set_output(self, output: dict[str, Any]) -> None:
        import opik

        opik.update_current_span(output=output)

    def set_usage(
        self, *, model: str, provider: str, usage: dict[str, int], total_cost: float
    ) -> None:
        import opik

        opik.update_current_span(model=model, provider=provider, usage=usage, total_cost=total_cost)


@dataclass
class OpikBackend:
    project_name: str

    @contextmanager
    def _span(
        self, name: str, tags: list[str], input: dict[str, Any], metadata: dict[str, Any]
    ) -> Iterator[SpanHandle]:
        import opik

        with opik.start_as_current_span(
            name=name,
            input=input,
            tags=tags,
            metadata=metadata,
            project_name=self.project_name,
        ):
            yield _OpikHandle()

    def span(
        self, name: str, *, tags: list[str], input: dict[str, Any], metadata: dict[str, Any]
    ) -> AbstractContextManager[SpanHandle]:
        return self._span(name, tags, input, metadata)

    @contextmanager
    def _llm_span(
        self, name: str, tags: list[str], input: dict[str, Any], metadata: dict[str, Any]
    ) -> Iterator[LLMSpanHandle]:
        import opik

        with opik.start_as_current_span(
            name=name,
            type="llm",
            input=input,
            tags=tags,
            metadata=metadata,
            project_name=self.project_name,
        ):
            yield _OpikHandle()

    def llm_span(
        self, name: str, *, tags: list[str], input: dict[str, Any], metadata: dict[str, Any]
    ) -> AbstractContextManager[LLMSpanHandle]:
        return self._llm_span(name, tags, input, metadata)

    def flush(self) -> None:
        import opik

        opik.flush_tracker()


@dataclass
class _State:
    backend: TraceBackend | None = None


_state = _State()


def set_backend(backend: TraceBackend | None) -> None:
    """Install a backend explicitly (tests use a recording fake)."""
    _state.backend = backend


def tracing_enabled() -> bool:
    return _state.backend is not None


def configure_tracing(settings: Settings) -> bool:
    """Enable Opik tracing when configured. Never raises; returns whether tracing is on."""
    key = settings.opik_api_key.get_secret_value() if settings.opik_api_key else ""
    if not (settings.opik_enabled and key):
        _state.backend = None
        log.info("tracing.disabled", reason="opik_enabled is false or OPIK_API_KEY missing")
        return False
    try:
        apply_opik_env(settings)
        import opik

        opik.set_tracing_active(True)
        _state.backend = OpikBackend(settings.opik_project_name)
    except Exception as exc:  # pragma: no cover - depends on the environment
        log.warning("tracing.init_failed", error=type(exc).__name__)
        _state.backend = None
        return False
    log.info("tracing.enabled", project=settings.opik_project_name)
    return True


def flush_tracing() -> None:
    if _state.backend is not None:
        try:
            _state.backend.flush()
        except Exception as exc:
            log.warning("tracing.flush_failed", error=type(exc).__name__)


def _base_metadata() -> dict[str, Any]:
    ctx = structlog.contextvars.get_contextvars()
    return {"request_id": request_id_var.get(), "role": ctx.get("user_role")}


def _summarise(fn: Callable[..., dict[str, Any]] | None, *args: Any, **kwargs: Any) -> Any:
    if fn is None:
        return {}
    try:
        return safe_payload(fn(*args, **kwargs))
    except Exception as exc:
        log.warning("tracing.summarise_failed", error=type(exc).__name__)
        return {}


def traced(
    name: str,
    *,
    tags: list[str] | None = None,
    inputs: Callable[..., dict[str, Any]] | None = None,
    outputs: Callable[[Any], dict[str, Any]] | None = None,
) -> Callable[[Callable[P, R]], Callable[P, R]]:
    """Trace a call as an Opik span. `inputs` receives the call's args; `outputs` its result."""
    span_tags = ["netracare", *(tags or [])]

    def deco(fn: Callable[P, R]) -> Callable[P, R]:
        @functools.wraps(fn)
        def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            backend = _state.backend
            if backend is None:
                return fn(*args, **kwargs)
            try:
                cm = backend.span(
                    name,
                    tags=span_tags,
                    input=_summarise(inputs, *args, **kwargs),
                    metadata=safe_payload(_base_metadata()),
                )
                handle = cm.__enter__()
            except Exception as exc:
                log.warning("tracing.span_failed", span=name, error=type(exc).__name__)
                return fn(*args, **kwargs)
            try:
                result = fn(*args, **kwargs)
            except Exception as exc:
                _close(
                    cm, handle, {"error": type(exc).__name__, "code": getattr(exc, "code", None)}
                )
                raise
            _close(cm, handle, _summarise(outputs, result) if outputs else {"ok": True})
            return result

        return wrapper

    return deco


def _close(
    cm: AbstractContextManager[SpanHandle], handle: SpanHandle, output: dict[str, Any]
) -> None:
    try:
        handle.set_output(output)
    except Exception as exc:
        log.warning("tracing.output_failed", error=type(exc).__name__)
    try:
        cm.__exit__(None, None, None)
    except Exception as exc:
        log.warning("tracing.close_failed", error=type(exc).__name__)


class _NullLLMSpan:
    def set_output(self, output: dict[str, Any]) -> None:
        return None

    def set_usage(
        self, *, model: str, provider: str, usage: dict[str, int], total_cost: float
    ) -> None:
        return None


class LLMSpan:
    """Handle yielded by `llm_span`. All methods are safe no-ops when tracing is off and
    swallow backend errors; payloads go through `safe_payload`."""

    def __init__(self, handle: LLMSpanHandle) -> None:
        self._handle = handle

    def set_usage(
        self, *, model: str, provider: str, input_tokens: int, output_tokens: int, cost_usd: float
    ) -> None:
        try:
            self._handle.set_usage(
                model=model,
                provider=provider,
                usage={
                    "prompt_tokens": input_tokens,
                    "completion_tokens": output_tokens,
                    "total_tokens": input_tokens + output_tokens,
                },
                total_cost=cost_usd,
            )
        except Exception as exc:
            log.warning("tracing.usage_failed", error=type(exc).__name__)

    def set_output(self, output: dict[str, Any]) -> None:
        try:
            self._handle.set_output(safe_payload(output))
        except Exception as exc:
            log.warning("tracing.output_failed", error=type(exc).__name__)


@contextmanager
def llm_span(
    name: str, *, model: str, prompt: str, purpose: str, input: dict[str, Any] | None = None
) -> Iterator[LLMSpan]:
    """An Opik `type="llm"` span for one LLM call. `input` must be a *summary* (lengths, ids),
    never prompt text; it is passed through `safe_payload` regardless."""
    backend = _state.backend
    if backend is None:
        yield LLMSpan(_NullLLMSpan())
        return
    payload = safe_payload({"purpose": purpose, "prompt": prompt, **(input or {})})
    metadata = safe_payload({**_base_metadata(), "model": model, "prompt": prompt})
    try:
        cm = backend.llm_span(
            name, tags=["netracare", "llm", purpose], input=payload, metadata=metadata
        )
        handle = cm.__enter__()
    except Exception as exc:
        log.warning("tracing.span_failed", span=name, error=type(exc).__name__)
        yield LLMSpan(_NullLLMSpan())
        return
    error: BaseException | None = None
    try:
        yield LLMSpan(handle)
    except BaseException as exc:
        error = exc
        raise
    finally:
        if error is not None:
            try:
                handle.set_output(
                    {"error": type(error).__name__, "code": getattr(error, "code", None)}
                )
            except Exception as exc:  # pragma: no cover - defensive
                log.warning("tracing.output_failed", error=type(exc).__name__)
        try:
            cm.__exit__(None, None, None)
        except Exception as exc:
            log.warning("tracing.close_failed", error=type(exc).__name__)
