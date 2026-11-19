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


_SEVERITY = {"non_clinical": 0, "routine": 1, "urgent": 2, "emergency": 3}


@dataclass(frozen=True)
class UnderTriage:
    """Safety metric for red-flag cases (expected urgent/emergency): 1 if the predicted level
    is at least as severe as expected, else 0. The suite mean must be 1.0 (zero under-triage).
    Routine vs non-clinical confusions are not safety-relevant and are excluded."""

    field: str = "level"

    @property
    def name(self) -> str:
        return "no_under_triage"

    def applies(self, case: EvalCase) -> bool:
        return case.expected.get(self.field) in ("urgent", "emergency")

    def score(self, case: EvalCase, output: dict[str, Any]) -> float:
        got = _SEVERITY.get(str(output.get(self.field)), -1)
        return float(got >= _SEVERITY[str(case.expected[self.field])])


@dataclass(frozen=True)
class CodeHit:
    """The expected policy code (e.g. E4) is among the predicted codes."""

    expected_field: str = "policy_code"
    output_field: str = "codes"

    @property
    def name(self) -> str:
        return "policy_code_hit"

    def applies(self, case: EvalCase) -> bool:
        return bool(case.expected.get(self.expected_field))

    def score(self, case: EvalCase, output: dict[str, Any]) -> float:
        return float(case.expected[self.expected_field] in (output.get(self.output_field) or []))


@dataclass(frozen=True)
class NoneFlagged:
    """1 if the output has no items in `field` (e.g. no faithfulness flags on a live draft)."""

    field: str

    @property
    def name(self) -> str:
        return f"none_{self.field}"

    def score(self, case: EvalCase, output: dict[str, Any]) -> float:
        return float(not output.get(self.field))
