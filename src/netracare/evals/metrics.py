"""Deterministic metrics. LLM-as-judge metrics arrive in Phase 5 behind the same protocol."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from netracare.evals.schema import EvalCase


class Metric(Protocol):
    @property
    def name(self) -> str: ...

    def score(self, case: EvalCase, output: dict[str, Any]) -> float:
        """Return a score in [0, 1] for one case."""
        ...


@dataclass(frozen=True)
class ExactMatch:
    field: str

    @property
    def name(self) -> str:
        return f"exact_{self.field}"

    def score(self, case: EvalCase, output: dict[str, Any]) -> float:
        return float(output.get(self.field) == case.expected.get(self.field))


@dataclass(frozen=True)
class SetEquality:
    field: str

    @property
    def name(self) -> str:
        return f"set_{self.field}"

    def score(self, case: EvalCase, output: dict[str, Any]) -> float:
        got = set(output.get(self.field) or [])
        want = set(case.expected.get(self.field) or [])
        return float(got == want)


@dataclass(frozen=True)
class LabelRecall:
    """Of the cases whose expected `field` equals `label`, the share predicted as `label`.

    Cases with a different expected label score None (excluded from the mean) - this is how
    "emergency recall" is computed in Phase 2+.
    """

    field: str
    label: str

    @property
    def name(self) -> str:
        return f"recall_{self.field}_{self.label}"

    def score(self, case: EvalCase, output: dict[str, Any]) -> float:
        return float(output.get(self.field) == self.label)

    def applies(self, case: EvalCase) -> bool:
        return bool(case.expected.get(self.field) == self.label)


def applies(metric: Metric, case: EvalCase) -> bool:
    check = getattr(metric, "applies", None)
    return bool(check(case)) if callable(check) else True
