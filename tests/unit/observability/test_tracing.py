from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime
from typing import Any

import pytest

from netracare.config import Settings
from netracare.domain.enums import Role
from netracare.log import request_id_var
from netracare.observability import tracing
from netracare.observability.tracing import (
    configure_tracing,
    flush_tracing,
    safe_payload,
    set_backend,
    traced,
    tracing_enabled,
)


class _Handle:
    def __init__(self, record: dict[str, Any]) -> None:
        self.record = record

    def set_output(self, output: dict[str, Any]) -> None:
        self.record["output"] = output


class RecordingBackend:
    def __init__(self, fail_on: str | None = None) -> None:
        self.spans: list[dict[str, Any]] = []
        self.flushed = 0
        self.fail_on = fail_on

    @contextmanager
    def _cm(self, record: dict[str, Any]) -> Iterator[_Handle]:
        if self.fail_on == "exit":
            yield _Handle(record)
            raise RuntimeError("backend exit failure")
        yield _Handle(record)

    def span(
        self, name: str, *, tags: list[str], input: dict[str, Any], metadata: dict[str, Any]
    ) -> Any:
        if self.fail_on == "span":
            raise RuntimeError("backend down")
        record = {"name": name, "tags": tags, "input": input, "metadata": metadata}
        self.spans.append(record)
        return self._cm(record)

    def flush(self) -> None:
        if self.fail_on == "flush":
            raise RuntimeError("flush failed")
        self.flushed += 1


@pytest.fixture
def backend() -> Iterator[RecordingBackend]:
    b = RecordingBackend()
    set_backend(b)
    yield b
    set_backend(None)


@traced(
    "demo.op",
    tags=["demo"],
    inputs=lambda patient, *, note=None: {"patient": patient, "note": note},
    outputs=lambda r: r,
)
def demo_op(patient: dict[str, Any], *, note: str | None = None) -> dict[str, Any]:
    return {"appointment_id": "a1", "status": "confirmed", "email": "x@example.com"}


@traced("demo.fail")
def demo_fail() -> None:
    raise ValueError("nope")


def test_traced_is_noop_when_disabled() -> None:
    set_backend(None)
    assert not tracing_enabled()
    assert demo_op({"id": "p1"})["status"] == "confirmed"


def test_configure_without_key_disabled() -> None:
    s = Settings(_env_file=None, opik_enabled=True)  # type: ignore[call-arg]
    assert configure_tracing(s) is False and not tracing_enabled()
    s2 = Settings(_env_file=None, opik_enabled=False, opik_api_key="k" * 20)  # type: ignore[call-arg,arg-type]
    assert configure_tracing(s2) is False


def test_exported_payload_has_no_pii(backend: RecordingBackend) -> None:
    patient = {
        "id": "p1",
        "full_name": "Asha Verma",
        "phone": "+91 98765 43210",
        "dob": date(1990, 5, 4),
        "nested": {"email": "asha@example.com", "free_text": "call me on +91 98765 43210"},
        "role": Role.PATIENT,
    }
    token = request_id_var.set("req-12345678")
    try:
        demo_op(patient, note="my email is asha@example.com")
    finally:
        request_id_var.reset(token)
    (span,) = backend.spans
    blob = repr(span)
    for needle in ("Asha", "98765", "asha@example.com", "1990"):
        assert needle not in blob, needle
    assert span["input"]["patient"]["id"] == "p1"
    assert span["input"]["patient"]["role"] == "patient"
    assert span["input"]["patient"]["nested"]["free_text"] == "call me on [PHONE]"
    assert "email" not in span["output"] and span["output"]["status"] == "confirmed"
    assert span["metadata"]["request_id"] == "req-12345678"
    assert span["tags"] == ["netracare", "demo"]


def test_errors_are_recorded_and_reraised(backend: RecordingBackend) -> None:
    with pytest.raises(ValueError, match="nope"):
        demo_fail()
    assert backend.spans[0]["output"] == {"error": "ValueError", "code": None}


def test_tracing_failure_never_breaks_the_operation() -> None:
    for mode in ("span", "exit"):
        set_backend(RecordingBackend(fail_on=mode))
        try:
            assert demo_op({"id": "p1"})["status"] == "confirmed"
        finally:
            set_backend(None)


def test_summariser_failure_is_tolerated(backend: RecordingBackend) -> None:
    @traced("demo.bad_inputs", inputs=lambda *a, **k: 1 / 0)  # type: ignore[arg-type,misc]
    def op() -> int:
        return 7

    assert op() == 7
    assert backend.spans[0]["input"] == {}


def test_flush(backend: RecordingBackend) -> None:
    flush_tracing()
    assert backend.flushed == 1
    set_backend(RecordingBackend(fail_on="flush"))
    flush_tracing()  # logged, not raised
    set_backend(None)
    flush_tracing()  # disabled: no-op


def test_safe_payload_shapes() -> None:
    out = safe_payload(
        {
            "when": datetime(2026, 1, 1, tzinfo=UTC),
            "ids": ("a", "b"),
            "n": 3,
            "ok": True,
            "none": None,
            "obj": object(),
            "Password": "secret",
            "token": "t",
        }
    )
    assert out["when"] == "2026-01-01T00:00:00+00:00"
    assert out["ids"] == ["a", "b"] and out["n"] == 3 and out["ok"] is True and out["none"] is None
    assert "Password" not in out and "token" not in out
    assert isinstance(out["obj"], str)
    deep: dict[str, Any] = {}
    cur = deep
    for _ in range(10):
        cur["k"] = {}
        cur = cur["k"]
    assert "[TRUNCATED]" in repr(safe_payload(deep))


def test_booking_service_is_instrumented() -> None:
    from netracare.services.approval_service import ApprovalService
    from netracare.services.availability_service import AvailabilityService
    from netracare.services.booking_service import BookingService

    for fn in (
        BookingService.book,
        BookingService.cancel,
        BookingService.reschedule,
        ApprovalService.approve,
        ApprovalService.reject,
        AvailabilityService.find,
    ):
        assert hasattr(fn, "__wrapped__"), fn.__qualname__


def test_opik_backend_is_lazy() -> None:
    b = tracing.OpikBackend("netracare")
    assert b.project_name == "netracare"
