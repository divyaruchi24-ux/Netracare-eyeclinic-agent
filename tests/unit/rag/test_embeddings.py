import math

import pytest

from netracare.config import get_settings
from netracare.rag.embeddings import (
    BGE_QUERY_PREFIX,
    FastEmbedder,
    FastReranker,
    HashingEmbedder,
    OverlapReranker,
    sigmoid,
)


def _cos(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b, strict=True))


def test_hashing_embedder_is_deterministic_and_normalised() -> None:
    e = HashingEmbedder(dim=128)
    a = e.embed_query("How much is LASIK?")
    assert a == HashingEmbedder(dim=128).embed_query("How much is LASIK?")
    assert len(a) == 128
    assert math.isclose(sum(x * x for x in a), 1.0, rel_tol=1e-9)
    docs = e.embed_documents(["LASIK price list", "parking for visitors"])
    assert _cos(a, docs[0]) > _cos(a, docs[1])


def test_hashing_embedder_empty_text() -> None:
    assert set(HashingEmbedder(dim=8).embed_query("")) == {0.0}


def test_overlap_reranker_orders_by_overlap() -> None:
    scores = OverlapReranker().score("lasik price", ["lasik price list", "parking", "lasik"])
    assert scores[0] > scores[2] > scores[1]
    assert OverlapReranker().score("q", []) == []


def test_sigmoid() -> None:
    assert sigmoid(0) == 0.5
    assert sigmoid(10) > 0.99 > 0.01 > sigmoid(-10)


def test_fast_models_empty_inputs_skip_loading() -> None:
    assert FastEmbedder("unused", "unused").embed_documents([]) == []
    assert FastReranker("unused", "unused").score("q", []) == []


@pytest.mark.models
def test_fast_embedder_semantics_and_query_prefix() -> None:
    s = get_settings()
    e = FastEmbedder(s.embedding_model, s.models_cache_dir)
    q = e.embed_query("how much does lasik cost")
    docs = e.embed_documents(["LASIK (both eyes) costs 52,000 rupees", "Free parking is available"])
    assert len(q) == 384
    assert _cos(q, docs[0]) > _cos(q, docs[1])
    # The bge instruction prefix is applied to queries only.
    assert e.embed_query("x") != e.embed_documents(["x"])[0]
    assert BGE_QUERY_PREFIX.startswith("Represent")


@pytest.mark.models
def test_fast_reranker_prefers_relevant_passage() -> None:
    s = get_settings()
    r = FastReranker(s.reranker_model, s.models_cache_dir)
    good, bad = r.score("how much is lasik", ["LASIK costs 52,000 rupees", "Parking is free"])
    assert good > 0 > bad
