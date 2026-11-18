"""CI regression gate: compare a run's aggregate metrics against a committed baseline."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from netracare.evals.runner import ExperimentReport

BASELINES_DIR = Path("evals/baselines")


@dataclass(frozen=True)
class Regression:
    metric: str
    baseline: float
    actual: float | None

    def __str__(self) -> str:
        got = "missing" if self.actual is None else f"{self.actual:.4f}"
        return f"{self.metric}: {got} < baseline {self.baseline:.4f}"


def baseline_path(suite: str, root: Path = Path()) -> Path:
    return root / BASELINES_DIR / f"{suite}.json"


def load_baseline(path: Path) -> dict[str, float]:
    data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return {k: float(v) for k, v in data["metrics"].items()}


def compare(
    report: ExperimentReport, baseline: dict[str, float], tolerance: float = 0.0
) -> list[Regression]:
    out: list[Regression] = []
    for metric, base in sorted(baseline.items()):
        actual = report.metrics.get(metric)
        if actual is None or actual < base - tolerance:
            out.append(Regression(metric, base, actual))
    return out


def write_baseline(report: ExperimentReport, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    body = {
        "suite": report.suite,
        "dataset_version": report.dataset_version,
        "n_cases": report.n_cases,
        "metrics": report.metrics,
        "git_sha": report.metadata.get("git_sha"),
    }
    path.write_text(json.dumps(body, indent=2) + "\n", encoding="utf-8")
