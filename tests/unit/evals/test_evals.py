from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from netracare.config import Settings
from netracare.evals import __main__ as cli
from netracare.evals.gate import compare, load_baseline, write_baseline
from netracare.evals.metrics import ExactMatch, LabelRecall, SetEquality
from netracare.evals.runner import config_fingerprint, run_suite, score_cases, upload_to_opik
from netracare.evals.schema import DatasetError, EvalCase, load_dataset
from netracare.evals.tasks import SUITES, booking_policy_task

ROOT = Path(__file__).resolve().parents[3]
SETTINGS = Settings(_env_file=None)  # type: ignore[call-arg]


def _write(tmp_path: Path, rows: list[dict[str, Any]] | str) -> Path:
    p = tmp_path / "ds.jsonl"
    text = rows if isinstance(rows, str) else "\n".join(json.dumps(r) for r in rows)
    p.write_text(text, encoding="utf-8")
    return p


def test_load_dataset_rejects_duplicate_ids(tmp_path: Path) -> None:
    row = {"id": "a", "input": {}, "expected": {}}
    with pytest.raises(DatasetError, match="duplicate"):
        load_dataset(_write(tmp_path, [row, row]))


@pytest.mark.parametrize(
    "content", ["", "not json", json.dumps({"id": "a", "input": {}, "expected": {}, "x": 1})]
)
def test_load_dataset_rejects_bad_content(tmp_path: Path, content: str) -> None:
    with pytest.raises(DatasetError):
        load_dataset(_write(tmp_path, content))


def test_dataset_version_tracks_content(tmp_path: Path) -> None:
    a = load_dataset(_write(tmp_path, [{"id": "a", "input": {}, "expected": {}}]))
    b = load_dataset(_write(tmp_path, [{"id": "a", "input": {"x": 1}, "expected": {}}]))
    assert a.version != b.version and len(a) == 1


def test_metrics() -> None:
    case = EvalCase(id="c", input={}, expected={"level": "emergency", "reasons": ["b", "a"]})
    assert ExactMatch("level").score(case, {"level": "emergency"}) == 1.0
    assert ExactMatch("level").score(case, {"level": "urgent"}) == 0.0
    assert SetEquality("reasons").score(case, {"reasons": ["a", "b"]}) == 1.0
    assert SetEquality("reasons").score(case, {"reasons": ["a"]}) == 0.0
    recall = LabelRecall("level", "emergency")
    assert recall.applies(case) and recall.score(case, {"level": "emergency"}) == 1.0
    other = EvalCase(id="d", input={}, expected={"level": "routine"})
    assert not recall.applies(other)


def test_score_cases_handles_crashes_and_recall() -> None:
    cases = (
        EvalCase(id="1", input={"v": "emergency"}, expected={"level": "emergency"}),
        EvalCase(id="2", input={"v": "routine"}, expected={"level": "emergency"}),
        EvalCase(id="3", input={"v": "boom"}, expected={"level": "routine"}),
        EvalCase(id="4", input={"v": "routine"}, expected={"level": "routine"}),
    )

    def task(inp: dict[str, Any]) -> dict[str, Any]:
        if inp["v"] == "boom":
            raise RuntimeError("model down")
        return {"level": inp["v"]}

    results, means, failures = score_cases(
        cases, task, [ExactMatch("level"), LabelRecall("level", "emergency")]
    )
    assert means == {"exact_level": 0.5, "recall_level_emergency": 0.5}
    assert failures == ["2", "3"]
    assert results[2].error == "RuntimeError: model down"


def test_booking_policy_dataset_passes_100pct(tmp_path: Path) -> None:
    report = run_suite("booking_policy", SETTINGS, root=ROOT, write_report=False)
    assert report.n_cases >= 40
    assert report.metrics == {"set_reasons": 1.0, "exact_auto_confirm": 1.0}, report.failures
    assert report.metadata["dataset_version"] == report.dataset_version
    assert len(report.metadata["config_fingerprint"]) == 12


def test_booking_policy_task_boundary() -> None:
    base = {
        "appointment_type": "routine",
        "is_change": True,
        "is_new_patient": False,
        "patient_is_minor": False,
        "booked_by_guardian": False,
        "red_flag_level": None,
        "no_show_count": 0,
    }
    assert booking_policy_task({**base, "hours_until_start": 24})["auto_confirm"] is True
    assert booking_policy_task({**base, "hours_until_start": 23.99})["reasons"] == [
        "within_24h_change"
    ]


def test_safety_dataset_schema_and_coverage() -> None:
    ds = load_dataset(ROOT / "evals/datasets/safety_redflags_v0.jsonl")
    codes = {c.expected.get("policy_code") for c in ds.cases}
    required = {f"E{i}" for i in range(1, 11)} | {f"U{i}" for i in range(1, 7)}
    assert required <= codes, sorted(required - codes)
    levels = {c.expected["level"] for c in ds.cases}
    assert levels == {"emergency", "urgent", "routine", "non_clinical"}
    assert any("hidden_emergency" in c.tags for c in ds.cases)
    assert any("prompt_injection" in c.tags for c in ds.cases)
    assert len(ds) >= 30


def test_unscored_suite_reports_schema_only() -> None:
    report = run_suite("safety_redflags_v0", SETTINGS, root=ROOT, write_report=False)
    assert report.scored is False and report.n_cases >= 30 and report.metrics == {}


def test_runner_writes_local_report(tmp_path: Path) -> None:
    (tmp_path / "evals/datasets").mkdir(parents=True)
    src = ROOT / "evals/datasets/booking_policy.jsonl"
    (tmp_path / "evals/datasets/booking_policy.jsonl").write_bytes(src.read_bytes())
    report = run_suite("booking_policy", SETTINGS, root=tmp_path, experiment_name="exp-1")
    out = tmp_path / "reports/evals/exp-1.json"
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["metrics"] == report.metrics and data["metadata"]["git_sha"]


def test_unknown_suite() -> None:
    with pytest.raises(KeyError):
        run_suite("nope", SETTINGS, root=ROOT)


def test_gate_detects_regression(tmp_path: Path) -> None:
    report = run_suite("booking_policy", SETTINGS, root=ROOT, write_report=False)
    assert compare(report, {"set_reasons": 1.0}) == []
    regs = compare(report, {"set_reasons": 1.01, "missing_metric": 0.5})
    assert [r.metric for r in regs] == ["missing_metric", "set_reasons"]
    assert "missing" in str(regs[0]) and "baseline" in str(regs[1])
    assert compare(report, {"set_reasons": 1.01}, tolerance=0.05) == []
    path = tmp_path / "b.json"
    write_baseline(report, path)
    assert load_baseline(path) == report.metrics


def test_committed_baseline_matches_current_results() -> None:
    report = run_suite("booking_policy", SETTINGS, root=ROOT, write_report=False)
    assert compare(report, load_baseline(ROOT / "evals/baselines/booking_policy.json")) == []


def test_cli_fail_under_exit_code(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    strict = tmp_path / "strict.json"
    strict.write_text(json.dumps({"metrics": {"set_reasons": 1.5}}), encoding="utf-8")
    args = ["--root", str(ROOT), "run", "booking_policy", "--no-report"]
    assert cli.main([*args, "--baseline", str(strict), "--fail-under"]) == 1
    assert "REGRESSION" in capsys.readouterr().out
    assert cli.main([*args, "--baseline", str(strict)]) == 0  # report-only without the flag
    assert cli.main(args) == 0
    assert cli.main(["--root", str(ROOT), "run", "nope"]) == 2
    assert cli.main(["--root", str(ROOT), "list"]) == 0
    assert cli.main(["--root", str(ROOT), "run", "all", "--no-report"]) == 0


def test_cli_update_baseline(tmp_path: Path) -> None:
    (tmp_path / "evals/datasets").mkdir(parents=True)
    for name in ("booking_policy",):
        src = ROOT / f"evals/datasets/{name}.jsonl"
        (tmp_path / f"evals/datasets/{name}.jsonl").write_bytes(src.read_bytes())
    assert cli.main(["--root", str(tmp_path), "update-baseline", "booking_policy"]) == 0
    assert (tmp_path / "evals/baselines/booking_policy.json").exists()
    missing = ["--root", str(tmp_path / "nowhere"), "run", "booking_policy"]
    assert cli.main(missing) == 2


def test_upload_skipped_without_key() -> None:
    report = run_suite("booking_policy", SETTINGS, root=ROOT, write_report=False)
    assert (
        upload_to_opik(
            SUITES["booking_policy"],
            load_dataset(ROOT / "evals/datasets/booking_policy.jsonl"),
            report,
            SETTINGS,
        )
        is None
    )


def test_config_fingerprint_changes_with_models() -> None:
    other = SETTINGS.model_copy(update={"judge_model": "another"})
    assert config_fingerprint(SETTINGS) != config_fingerprint(other)
