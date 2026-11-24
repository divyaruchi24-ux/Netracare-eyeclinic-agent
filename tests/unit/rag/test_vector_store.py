from collections.abc import Iterator
from pathlib import Path

import pytest

from netracare.rag.vector_store import (
    ChromaVectorStore,
    InMemoryVectorStore,
    VectorStore,
    collection_name,
)


@pytest.fixture(params=["memory", "chroma"])
def store(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[VectorStore]:
    if request.param == "memory":
        yield InMemoryVectorStore()
    else:
        yield ChromaVectorStore(str(tmp_path / "chroma"), "kb_test_c1")


def _fill(store: VectorStore) -> None:
    store.upsert(
        ["a", "b", "c"],
        [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.7071, 0.7071, 0.0]],
        [
            {"is_current": True, "doc_type": "price_list", "doctor_code": None},
            {"is_current": False, "doc_type": "price_list", "doctor_code": None},
            {"is_current": True, "doc_type": "doctor_bio", "doctor_code": "D2"},
        ],
    )


def test_query_orders_by_cosine(store: VectorStore) -> None:
    _fill(store)
    hits = store.query([1.0, 0.0, 0.0], 3)
    assert [i for i, _ in hits] == ["a", "c", "b"]
    assert hits[0][1] == pytest.approx(1.0, abs=1e-4)
    assert hits[1][1] == pytest.approx(0.7071, abs=1e-3)


def test_filters(store: VectorStore) -> None:
    _fill(store)
    assert [i for i, _ in store.query([0.0, 1.0, 0.0], 5, {"is_current": True})] == ["c", "a"]
    assert [i for i, _ in store.query([1.0, 0.0, 0.0], 5, {"doc_type": ["doctor_bio"]})] == ["c"]
    both = {"is_current": True, "doc_type": "price_list"}
    assert [i for i, _ in store.query([0.0, 1.0, 0.0], 5, both)] == ["a"]


def test_upsert_replaces_delete_count_ids_reset(store: VectorStore) -> None:
    _fill(store)
    store.upsert(["a"], [[0.0, 0.0, 1.0]], [{"is_current": True}])
    assert store.query([0.0, 0.0, 1.0], 1)[0][0] == "a"
    assert store.count() == 3 and store.ids() == {"a", "b", "c"}
    store.delete(["a", "b"])
    store.delete([])
    assert store.ids() == {"c"}
    store.reset()
    assert store.count() == 0
    assert store.query([1.0, 0.0, 0.0], 3) == []


def test_fingerprint_round_trip(store: VectorStore) -> None:
    assert store.get_fingerprint() is None
    store.set_fingerprint("abc123")
    assert store.get_fingerprint() == "abc123"


def test_chroma_persists_across_clients(tmp_path: Path) -> None:
    path = str(tmp_path / "chroma")
    a = ChromaVectorStore(path, "kb_test_c1")
    _fill(a)
    a.set_fingerprint("fp")
    b = ChromaVectorStore(path, "kb_test_c1")
    assert b.count() == 3 and b.get_fingerprint() == "fp"


def test_collection_name_is_safe_and_versioned() -> None:
    name = collection_name("BAAI/bge-small-en-v1.5", "1")
    assert name == "kb_baai_bge_small_en_v1_5_c1"
    assert len(collection_name("x" * 200, "1")) <= 60
