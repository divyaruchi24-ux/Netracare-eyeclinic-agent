"""Shared helpers for LLM tests: canned provider responses and a recording trace backend."""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from netracare.llm.types import ProviderResponse


def resp(
    payload: dict[str, Any] | str, *, tin: int = 100, tout: int = 50, model: str = ""
) -> ProviderResponse:
    text = payload if isinstance(payload, str) else json.dumps(payload)
    return ProviderResponse(text=text, input_tokens=tin, output_tokens=tout, model=model)


def triage_json(
    level: str = "routine",
    codes: list[str] | None = None,
    specialty: str | None = "GENERAL",
    confidence: float = 0.9,
    concern: str = "eye concern",
    medical_question: bool = False,
) -> dict[str, Any]:
    return {
        "level": level,
        "matched_codes": codes or [],
        "suggested_specialty": specialty,
        "confidence": confidence,
        "concern_phrase": concern,
        "is_medical_question": medical_question,
    }


class _Handle:
    def __init__(self, record: dict[str, Any]) -> None:
        self.record = record

    def set_output(self, output: dict[str, Any]) -> None:
        self.record["output"] = output

    def set_usage(
        self, *, model: str, provider: str, usage: dict[str, int], total_cost: float
    ) -> None:
        self.record["usage"] = {"model": model, "provider": provider, **usage, "cost": total_cost}


class RecordingBackend:
    def __init__(self) -> None:
        self.spans: list[dict[str, Any]] = []

    @contextmanager
    def _cm(self, record: dict[str, Any]) -> Iterator[_Handle]:
        yield _Handle(record)

    def _add(self, name: str, kind: str, **kw: Any) -> Any:
        record = {"name": name, "type": kind, **kw}
        self.spans.append(record)
        return self._cm(record)

    def span(
        self, name: str, *, tags: list[str], input: dict[str, Any], metadata: dict[str, Any]
    ) -> Any:
        return self._add(name, "general", tags=tags, input=input, metadata=metadata)

    def llm_span(
        self, name: str, *, tags: list[str], input: dict[str, Any], metadata: dict[str, Any]
    ) -> Any:
        return self._add(name, "llm", tags=tags, input=input, metadata=metadata)

    def flush(self) -> None:
        return None
