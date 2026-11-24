import json

import pytest
from sqlalchemy.orm import Session, sessionmaker

from netracare.config import Settings
from netracare.observability.tracing import set_backend
from netracare.rag.embeddings import HashingEmbedder, OverlapReranker
from netracare.rag.retriever import (
    IndexStaleError,
    RetrievalFilters,
    RetrievalIndex,
    normalize_query,
)
from netracare.rag.vector_store import InMemoryVectorStore
from tests.fixtures_rag import hashing_store_from_db
from tests.llm_helpers import RecordingBackend


def _settings(**kw: object) -> Settings:
    return Settings(_env_file=None, **kw)  # type: ignore[call-arg, arg-type]


def _index(factory: sessionmaker[Session], *, rerank: bool = False, **kw: object) -> RetrievalIndex:
    return RetrievalIndex(
        factory,
        HashingEmbedder(),
        hashing_store_from_db(factory),
        OverlapReranker() if rerank else None,
        _settings(rag_rerank=rerank, **kw),
    ).load()


def test_loads_records_and_vectors(kb_factory: sessionmaker[Session]) -> None:
    idx = _index(kb_factory)
    assert idx.vectors_ok and len(idx) > 70
    assert {d.code for d in idx.doctors} == {"D1", "D2", "D3", "D4", "D5"}


def test_hybrid_finds_lasik_price_with_typo(kb_factory: sessionmaker[Session]) -> None:
    r = _index(kb_factory).retrieve("how much is lasic?")
    assert r.mode == "hybrid"
    assert r.normalized_query == "how much is lasik?"
    slugs = [c.chunk.slug for c in r.chunks]
    assert "procedure-lasik" in slugs[:3] or "price-list" in slugs[:3]


def test_superseded_chunks_are_never_returned_by_default(
    kb_factory: sessionmaker[Session],
) -> None:
    idx = _index(kb_factory)
    for q in ["LASIK price 45,000", "price list 2025", "how much is cataract surgery"]:
        r = idx.retrieve(q, k=20)
        assert all(c.chunk.is_current for c in r.chunks), q
    old = idx.retrieve("price list 2025", RetrievalFilters(current_only=False), k=20)
    assert any(not c.chunk.is_current for c in old.chunks)


def test_hard_filters(kb_factory: sessionmaker[Session]) -> None:
    idx = _index(kb_factory)
    r = idx.retrieve("appointment", RetrievalFilters(doc_types=("doctor_bio",)), k=10)
    assert r.chunks and {c.chunk.doc_type for c in r.chunks} == {"doctor_bio"}
    r = idx.retrieve("visit", RetrievalFilters(doctor_code="D3"), k=10)
    assert r.chunks and {c.chunk.doctor_code for c in r.chunks} == {"D3"}


def test_pricing_boost_lifts_the_price_list(kb_factory: sessionmaker[Session]) -> None:
    idx = _index(kb_factory)
    boosted = idx.retrieve(
        "how much for a squint evaluation", RetrievalFilters(boost_doc_types=("price_list",))
    )
    assert boosted.chunks[0].chunk.doc_type == "price_list"


def test_fuzzy_doctor_boost(kb_factory: sessionmaker[Session]) -> None:
    r = _index(kb_factory).retrieve("Dr Shetti timings")
    assert r.doctor is not None and r.doctor.code == "D2"
    assert r.normalized_query == "Dr Shetty timing"
    assert r.chunks[0].chunk.slug == "dr-vikram-shetty"


def test_rerank_orders_and_sets_confidence(kb_factory: sessionmaker[Session]) -> None:
    idx = _index(kb_factory, rerank=True)
    r = idx.retrieve("what should I bring to my appointment")
    scores = [c.rerank_score for c in r.chunks]
    assert all(s is not None for s in scores)
    assert scores == sorted(scores, reverse=True)  # type: ignore[type-var]
    assert [c.rank for c in r.chunks] == list(range(1, len(r.chunks) + 1))
    assert 0.0 < r.confidence <= 1.0
    assert "rerank" in r.stages


def test_out_of_domain_query_has_lower_confidence(kb_factory: sessionmaker[Session]) -> None:
    idx = _index(kb_factory)
    in_kb = idx.retrieve("is parking available at the clinic").confidence
    off = idx.retrieve("quantum chromodynamics lecture notes").confidence
    assert in_kb > off


def test_empty_query(kb_factory: sessionmaker[Session]) -> None:
    r = _index(kb_factory).retrieve("   ")
    assert r.chunks == [] and r.confidence == 0.0


def test_stale_store_falls_back_to_bm25_only(kb_factory: sessionmaker[Session]) -> None:
    idx = RetrievalIndex(
        kb_factory, HashingEmbedder(), InMemoryVectorStore(), None, _settings()
    ).load()
    assert not idx.vectors_ok
    with pytest.raises(IndexStaleError):
        idx.require_fresh()
    r = idx.retrieve("parking")
    assert r.mode == "bm25_only" and r.chunks
    assert r.confidence == 0.0  # no vectors, no reranker: refuse rather than guess


def test_stale_store_with_reranker_still_has_confidence(
    kb_factory: sessionmaker[Session],
) -> None:
    idx = RetrievalIndex(
        kb_factory,
        HashingEmbedder(),
        InMemoryVectorStore(),
        OverlapReranker(),
        _settings(rag_rerank=True),
    ).load()
    r = idx.retrieve("parking and access")
    assert r.mode == "bm25_only" and r.confidence > 0


def test_broken_store_does_not_raise(kb_factory: sessionmaker[Session]) -> None:
    class Broken(InMemoryVectorStore):
        def get_fingerprint(self) -> str | None:
            raise RuntimeError("down")

    idx = RetrievalIndex(kb_factory, HashingEmbedder(), Broken(), None, _settings()).load()
    assert not idx.vectors_ok


def test_reranker_failure_falls_back_to_cosine(kb_factory: sessionmaker[Session]) -> None:
    class Failing(OverlapReranker):
        def score(self, query, texts):  # type: ignore[no-untyped-def]
            raise RuntimeError("boom")

    idx = RetrievalIndex(
        kb_factory,
        HashingEmbedder(),
        hashing_store_from_db(kb_factory),
        Failing(),
        _settings(rag_rerank=True),
    ).load()
    r = idx.retrieve("parking")
    assert r.chunks and r.chunks[0].rerank_score is None and r.confidence > 0


def test_chunk_lookup_and_reload(kb_factory: sessionmaker[Session]) -> None:
    idx = _index(kb_factory)
    any_id = idx.retrieve("parking").chunks[0].chunk.id
    assert idx.chunk(any_id) is not None
    assert idx.chunk("missing") is None
    assert idx.reload() is idx


def test_trace_span_has_ids_and_scores_but_no_text(kb_factory: sessionmaker[Session]) -> None:
    backend = RecordingBackend()
    set_backend(backend)
    try:
        _index(kb_factory).retrieve("my phone is 9876543210, how much is lasik")
    finally:
        set_backend(None)
    span = next(s for s in backend.spans if s["name"] == "rag.retrieve")
    blob = json.dumps(span, default=str)
    assert "9876543210" not in blob and "lasik" not in blob.lower().replace("procedure-lasik", "")
    assert span["input"]["query_chars"] > 0
    assert span["output"]["top"] and "id" in span["output"]["top"][0]


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("  catract  fees ", "cataract fees"), ("Glacoma test", "glaucoma test"), ("ok", "ok")],
)
def test_normalize_query(raw: str, expected: str) -> None:
    assert normalize_query(raw, None) == expected
