from netracare.config import get_settings
from netracare.evals.metrics import NoWriteWithoutYes, TaskSuccess, ToolCallCorrectness
from netracare.evals.runner import run_suite
from netracare.evals.schema import EvalCase, load_dataset
from tests.conftest import ROOT


def test_dataset_covers_the_corner_cases() -> None:
    ds = load_dataset(ROOT / "evals/datasets/booking_agent_v0.jsonl")
    tags = {t for c in ds.cases for t in c.tags}
    assert len(ds) >= 30
    for needed in (
        "hedged_yes",
        "slot_taken",
        "tool_failure",
        "within_24h",
        "guardian",
        "hitl_edit",
        "safety_pause",
        "abandon_resume",
        "closed_day",
        "out_of_hours",
    ):
        assert needed in tags, needed


def test_offline_suite_meets_the_phase_4_gate() -> None:
    report = run_suite("booking_agent_offline", get_settings(), root=ROOT, write_report=False)
    assert report.metrics["no_write_without_yes"] == 1.0
    assert report.metrics["task_success"] >= 0.95
    assert report.metrics["tool_call_correctness"] >= 0.95


def test_metrics() -> None:
    case = EvalCase(
        id="x",
        input={},
        expected={
            "final_step": "done",
            "writes_ok": ["book"],
            "tools": ["a", "b"],
            "final_kind": "confirmed",
        },
    )
    good = {
        "final_step": "done",
        "writes_ok": ["book"],
        "final_kind": "confirmed",
        "tool_calls": ["a", "x", "b"],
        "write_safe": True,
    }
    assert TaskSuccess().score(case, good) == 1.0
    assert TaskSuccess().score(case, {**good, "final_kind": "pending"}) == 0.0
    assert ToolCallCorrectness().score(case, good) == 1.0
    assert ToolCallCorrectness().score(case, {**good, "tool_calls": ["b", "a"]}) == 0.0
    assert NoWriteWithoutYes().score(case, {"write_safe": False}) == 0.0
