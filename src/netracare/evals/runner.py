"""Run an eval suite locally, write a JSON report, and (optionally) log an Opik experiment.

Run metadata (git SHA, dataset version, config fingerprint) makes experiments comparable:
two runs with the same fingerprint differ only in code.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from netracare.config import Settings
from netracare.evals.metrics import Metric, applies
from netracare.evals.schema import Dataset, EvalCase, dataset_path, load_dataset
from netracare.evals.tasks import SUITES, EvalSuite, Task
from netracare.log import get_logger
from netracare.observability.opik_config import apply_opik_env

log = get_logger(__name__)
REPORTS_DIR = Path("reports/evals")


@dataclass
class CaseResult:
    id: str
    scores: dict[str, float]
    output: dict[str, Any]
    error: str | None = None


@dataclass
class ExperimentReport:
    suite: str
    experiment_name: str
    dataset_version: str
    n_cases: int
    metrics: dict[str, float]
    metadata: dict[str, Any]
    failures: list[str] = field(default_factory=list)
    cases: list[CaseResult] = field(default_factory=list)
    scored: bool = True
    opik_experiment: str | None = None

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


def git_sha(root: Path = Path()) -> str:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],  # noqa: S607
            cwd=root,
            capture_output=True,
            text=True,
            check=True,
            timeout=5,
        )
        return out.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def config_fingerprint(settings: Settings) -> str:
    """Hash of everything (besides code) that can change eval results."""
    relevant = {
        "groq_model_reasoning": settings.groq_model_reasoning,
        "groq_model_fast": settings.groq_model_fast,
        "judge_provider": settings.judge_provider,
        "judge_model": settings.judge_model,
        "judge_reasoning_effort": settings.judge_reasoning_effort,
        "judge_max_tokens": settings.judge_max_tokens,
        "embedding_model": settings.embedding_model,
        "reranker_model": settings.reranker_model,
    }
    blob = json.dumps(relevant, sort_keys=True).encode()
    return hashlib.sha256(blob).hexdigest()[:12]


def score_cases(
    cases: tuple[EvalCase, ...], task: Task, metrics: list[Metric]
) -> tuple[list[CaseResult], dict[str, float], list[str]]:
    results: list[CaseResult] = []
    sums: dict[str, float] = {m.name: 0.0 for m in metrics}
    counts: dict[str, int] = {m.name: 0 for m in metrics}
    failures: list[str] = []
    for case in cases:
        try:
            output = task(case.input)
            error = None
        except Exception as exc:  # a crashing case scores 0 on every metric
            output, error = {}, f"{type(exc).__name__}: {exc}"
        scores: dict[str, float] = {}
        for m in metrics:
            if not applies(m, case):
                continue
            s = 0.0 if error else m.score(case, output)
            scores[m.name] = s
            sums[m.name] += s
            counts[m.name] += 1
        if error or any(v < 1.0 for v in scores.values()):
            failures.append(case.id)
        results.append(CaseResult(case.id, scores, output, error))
    means = {k: round(sums[k] / counts[k], 4) for k in sums if counts[k]}
    return results, means, failures


def run_suite(
    name: str,
    settings: Settings,
    *,
    root: Path = Path(),
    upload: bool = False,
    write_report: bool = True,
    experiment_name: str | None = None,
) -> ExperimentReport:
    if name not in SUITES:
        raise KeyError(f"unknown eval suite {name!r}; known: {sorted(SUITES)}")
    suite = SUITES[name]
    dataset = load_dataset(dataset_path(suite.dataset, root))
    sha = git_sha(root)
    exp_name = experiment_name or f"{name}-{sha}-{datetime.now(UTC):%Y%m%dT%H%M%S}"
    metadata = {
        "git_sha": sha,
        "dataset": dataset.name,
        "dataset_version": dataset.version,
        "config_fingerprint": config_fingerprint(settings),
        "models": {"reasoning": settings.groq_model_reasoning, "fast": settings.groq_model_fast},
        "judge": {"provider": settings.judge_provider, "model": settings.judge_model},
        "prompt_versions": {},  # populated once prompts exist (Phase 2)
    }
    if suite.task is None:
        report = ExperimentReport(
            name, exp_name, dataset.version, len(dataset), {}, metadata, scored=False
        )
    else:
        cases, means, failures = score_cases(dataset.cases, suite.task, suite.metrics)
        report = ExperimentReport(
            name, exp_name, dataset.version, len(dataset), means, metadata, failures, cases
        )
        if upload:
            report.opik_experiment = upload_to_opik(suite, dataset, report, settings)
    if write_report:
        out = root / REPORTS_DIR / f"{exp_name}.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report.to_json(), indent=2, default=str), encoding="utf-8")
        log.info("evals.report_written", path=str(out), metrics=report.metrics)
    return report


def upload_to_opik(
    suite: EvalSuite, dataset: Dataset, report: ExperimentReport, settings: Settings
) -> str | None:
    """Mirror the dataset to Opik and log the run as an experiment. Never raises."""
    key = settings.opik_api_key.get_secret_value() if settings.opik_api_key else ""
    if not key or suite.task is None:
        log.warning("evals.upload_skipped", reason="no OPIK_API_KEY or unscored suite")
        return None
    try:
        import opik
        from opik.evaluation import evaluate
        from opik.evaluation.metrics import base_metric, score_result

        apply_opik_env(settings)
        client = opik.Opik(project_name=settings.opik_project_name)
        ds = client.get_or_create_dataset(
            name=f"netracare-{dataset.name}", description=suite.description
        )
        ds.insert(
            [
                {"case_id": c.id, "input": c.input, "expected": c.expected, "tags": c.tags}
                for c in dataset.cases
            ]
        )
        by_id = {c.id: c for c in dataset.cases}
        task = suite.task

        class _Adapter(base_metric.BaseMetric):
            def __init__(self, metric: Metric) -> None:
                super().__init__(name=metric.name, track=False)
                self._m = metric

            def score(self, **kwargs: Any) -> score_result.ScoreResult:
                case = by_id[kwargs["case_id"]]
                if not applies(self._m, case):
                    return score_result.ScoreResult(name=self.name, value=1.0, reason="n/a")
                return score_result.ScoreResult(
                    name=self.name, value=self._m.score(case, kwargs.get("output") or {})
                )

        def opik_task(item: dict[str, Any]) -> dict[str, Any]:
            return {"output": task(item["input"]), "case_id": item["case_id"]}

        evaluate(
            dataset=ds,
            task=opik_task,
            scoring_metrics=[_Adapter(m) for m in suite.metrics],
            experiment_name=report.experiment_name,
            project_name=settings.opik_project_name,
            experiment_config=report.metadata,
            verbose=0,
            task_threads=4,
        )
        opik.flush_tracker()
        return report.experiment_name
    except Exception as exc:
        log.warning(
            "evals.upload_failed",
            error=type(exc).__name__,
            status=getattr(exc, "status_code", None),
            body=str(getattr(exc, "body", "") or exc)[:300],
        )
        return None
