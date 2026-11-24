from pathlib import Path

import pytest
from sqlalchemy.orm import Session, sessionmaker

from netracare.config import Settings
from netracare.evals.metrics import (
    MRR,
    InKbAnswerable,
    NoSupersededLeak,
    OutOfKbRefusal,
    RecallAtK,
    applies,
)
from netracare.evals.rag_tasks import retrieval_output
from netracare.evals.schema import EvalCase, load_dataset
from netracare.evals.tasks import SUITES
from netracare.rag.embeddings import HashingEmbedder
from netracare.rag.retriever import RetrievalIndex
from tests.conftest import ROOT
from tests.fixtures_rag import hashing_store_from_db

ANSWERABLE = EvalCase(id="a", input={"query": "q"}, expected={"slugs": ["x", "y"]})
REFUSE = EvalCase(id="r", input={"query": "q"}, expected={"refuse": True})


def test_recall_at_k() -> None:
    m = RecallAtK(2)
    assert m.name == "recall_at_2"
    assert m.score(ANSWERABLE, {"slugs": ["z", "y", "x"]}) == 1.0
    assert m.score(ANSWERABLE, {"slugs": ["z", "w", "x"]}) == 0.0
    assert applies(m, ANSWERABLE) and not applies(m, REFUSE)


def test_mrr() -> None:
    assert MRR().score(ANSWERABLE, {"slugs": ["z", "x"]}) == 0.5
    assert MRR().score(ANSWERABLE, {"slugs": []}) == 0.0
    assert not applies(MRR(), REFUSE)


def test_superseded_leak_applies_everywhere() -> None:
    m = NoSupersededLeak()
    assert applies(m, ANSWERABLE) and applies(m, REFUSE)
    assert m.score(REFUSE, {"all_current": True}) == 1.0
    assert m.score(REFUSE, {"all_current": False}) == 0.0
    assert m.score(REFUSE, {}) == 0.0


def test_refusal_and_answerable() -> None:
    assert OutOfKbRefusal().score(REFUSE, {"answerable": False}) == 1.0
    assert OutOfKbRefusal().score(REFUSE, {"answerable": True}) == 0.0
    assert not applies(OutOfKbRefusal(), ANSWERABLE)
    assert InKbAnswerable().score(ANSWERABLE, {"answerable": True}) == 1.0
    assert not applies(InKbAnswerable(), REFUSE)


def test_dataset_shape() -> None:
    ds = load_dataset(ROOT / "evals" / "datasets" / "rag_retrieval_v0.jsonl")
    refuse = [c for c in ds.cases if c.expected.get("refuse")]
    answerable = [c for c in ds.cases if c.expected.get("slugs")]
    assert len(ds) >= 40 and len(refuse) >= 8
    assert len(refuse) + len(answerable) == len(ds)
    assert sum("price_conflict" in c.tags for c in ds.cases) >= 3
    suite = SUITES["rag_retrieval"]
    assert suite.requires_models and not suite.requires_live


def test_expected_slugs_exist_in_kb() -> None:
    from netracare.rag.loaders import scan

    slugs = {d.slug for d in scan(ROOT / "kb")}
    ds = load_dataset(ROOT / "evals" / "datasets" / "rag_retrieval_v0.jsonl")
    for case in ds.cases:
        for s in case.expected.get("slugs", []):
            assert s in slugs, (case.id, s)


def test_retrieval_output_shape(kb_factory: sessionmaker[Session]) -> None:
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    idx = RetrievalIndex(
        kb_factory, HashingEmbedder(), hashing_store_from_db(kb_factory), None, settings
    ).load()
    out = retrieval_output(idx, {"query": "is there parking"}, threshold=0.0)
    assert out["slugs"] and out["all_current"] is True
    assert out["answerable"] is True and out["mode"] == "hybrid"
    assert len(out["chunk_ids"]) == len(out["slugs"])
    filtered = retrieval_output(
        idx, {"query": "price", "filters": {"doc_types": ["price_list"]}}, threshold=2.0
    )
    assert set(filtered["slugs"]) == {"price-list"} and filtered["answerable"] is False


@pytest.mark.models
def test_rag_retrieval_suite_meets_targets(tmp_path: Path) -> None:
    """The real offline gate (fastembed models): the Phase 3 exit criteria."""
    from netracare.config import get_settings
    from netracare.evals.runner import run_suite

    report = run_suite("rag_retrieval", get_settings(), root=ROOT, write_report=False)
    m = report.metrics
    assert m["recall_at_5"] >= 0.90
    assert m["no_superseded_leak"] == 1.0
    assert m["out_of_kb_refusal"] >= 0.85
    assert m["in_kb_answerable"] >= 0.95
