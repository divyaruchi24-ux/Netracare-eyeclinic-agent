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


def test_unscored_suite_reports_schema_only(monkeypatch: pytest.MonkeyPatch) -> None:
    from netracare.evals.tasks import EvalSuite

    monkeypatch.setitem(
        SUITES, "schema_only_demo", EvalSuite(dataset="safety_redflags_v0", task=None)
    )
    report = run_suite("schema_only_demo", SETTINGS, root=ROOT, write_report=False)
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


# -- Phase 2 suites ------------------------------------------------------------------------------


def _case(expected: dict[str, Any]) -> EvalCase:
    return EvalCase(id="x", input={}, expected=expected)


def test_under_triage_metric() -> None:
    from netracare.evals.metrics import UnderTriage

    m = UnderTriage()
    assert not m.applies(_case({"level": "routine"}))
    urgent = _case({"level": "urgent"})
    assert m.applies(urgent)
    assert m.score(urgent, {"level": "emergency"}) == 1.0
    assert m.score(urgent, {"level": "urgent"}) == 1.0
    assert m.score(urgent, {"level": "routine"}) == 0.0
    assert m.score(urgent, {}) == 0.0


def test_code_hit_and_none_flagged() -> None:
    from netracare.evals.metrics import CodeHit, NoneFlagged

    c = _case({"policy_code": "E4"})
    assert CodeHit().applies(c) and CodeHit().score(c, {"codes": ["E1", "E4"]}) == 1.0
    assert not CodeHit().applies(_case({"policy_code": None}))
    assert NoneFlagged("invented").score(c, {"invented": []}) == 1.0
    assert NoneFlagged("invented").score(c, {"invented": ["x"]}) == 0.0


def test_rules_suite_meets_safety_gate() -> None:
    report = run_suite("safety_redflags_rules", SETTINGS, root=ROOT, write_report=False)
    m = report.metrics
    assert m["recall_level_emergency"] == 1.0
    assert m["recall_level_urgent"] == 1.0
    assert m["no_under_triage"] == 1.0
    assert m["policy_code_hit"] == 1.0
    assert report.metadata["prompt_versions"]  # all prompts recorded when unspecified
    assert report.metadata["live"] is False


@pytest.mark.parametrize("suite", ["eye_fact_safety", "visit_summary_v0"])
def test_offline_llm_feature_suites_pass(suite: str) -> None:
    report = run_suite(suite, SETTINGS, root=ROOT, write_report=False)
    assert report.metrics and all(v == 1.0 for v in report.metrics.values()), report.failures


def test_live_suites_flagged_and_prompt_versions_scoped() -> None:
    from netracare.evals.runner import prompt_versions

    live = {n for n, s in SUITES.items() if s.requires_live}
    assert live == {
        "safety_redflags_llm",
        "visit_summary_live",
        "router_llm",
        "rag_answer_live",
        "booking_agent_live",
    }
    pv = prompt_versions(SETTINGS, SUITES["safety_redflags_llm"], ROOT)
    assert set(pv) == {"red_flag_classifier", "json_repair"}
    assert pv["red_flag_classifier"].startswith("v1@")


def test_cli_skips_live_suites_without_flag(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["--root", str(ROOT), "run", "safety_redflags_llm", "--no-report"]) == 0
    assert "skipped" in capsys.readouterr().out
    assert cli.main(["--root", str(ROOT), "run", "all", "--no-report"]) == 0
    out = capsys.readouterr().out
    assert "safety_redflags_rules" in out and "safety_redflags_llm" not in out
    assert cli.main(["--root", str(ROOT), "list"]) == 0
    assert "live" in capsys.readouterr().out


def test_prompt_versions_missing_dir(tmp_path: Path) -> None:
    from netracare.evals.runner import prompt_versions

    s = Settings(_env_file=None, prompts_dir=str(tmp_path / "none"))  # type: ignore[call-arg]
    assert prompt_versions(s, SUITES["booking_policy"], tmp_path) == {}


def test_upload_reuses_scored_outputs_without_rerunning_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import opik
    import opik.evaluation

    from netracare.evals.tasks import EvalSuite

    calls: list[dict[str, Any]] = []

    def counting_task(inp: dict[str, Any]) -> dict[str, Any]:
        calls.append(inp)
        return booking_policy_task(inp)

    suite = EvalSuite(
        dataset="booking_policy", task=counting_task, metrics=SUITES["booking_policy"].metrics
    )
    monkeypatch.setitem(SUITES, "counting_demo", suite)
    report = run_suite("counting_demo", SETTINGS, root=ROOT, write_report=False)
    n_local = len(calls)

    class FakeDataset:
        def __init__(self) -> None:
            self.rows: list[dict[str, Any]] = []

        def insert(self, rows: list[dict[str, Any]]) -> None:
            self.rows.extend(rows)

    ds = FakeDataset()
    captured: dict[str, Any] = {}

    class FakeClient:
        def __init__(self, **_kw: Any) -> None: ...

        def get_or_create_dataset(self, **_kw: Any) -> FakeDataset:
            return ds

    def fake_evaluate(**kw: Any) -> None:
        captured.update(kw)
        captured["outputs"] = [kw["task"](row) for row in ds.rows]

    monkeypatch.setattr(opik, "Opik", FakeClient)
    monkeypatch.setattr(opik, "flush_tracker", lambda: None)
    monkeypatch.setattr(opik.evaluation, "evaluate", fake_evaluate)
    for var in ("OPIK_API_KEY", "OPIK_PROJECT_NAME", "OPIK_WORKSPACE"):
        monkeypatch.setenv(var, "x")  # restored by monkeypatch after the test
    keyed = Settings(_env_file=None, opik_api_key="x", opik_workspace="test-ws")  # type: ignore[call-arg, arg-type]
    dataset = load_dataset(ROOT / "evals/datasets/booking_policy.jsonl")
    name = upload_to_opik(suite, dataset, report, keyed)  # explicit workspace: no network lookup
    assert name == report.experiment_name
    assert len(calls) == n_local  # no second run of the task
    by_id = {c.id: c.output for c in report.cases}
    assert all(o["output"] == by_id[o["case_id"]] for o in captured["outputs"])
    assert captured["experiment_config"]["dataset_version"] == report.dataset_version
