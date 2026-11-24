import pytest

from netracare.rag.bm25 import BM25Doc, BM25Index, tokenize

DOCS = [
    BM25Doc("lasik", "LASIK FAQ: what does it cost? See the price list.", {"is_current": True}),
    BM25Doc("price", "Price list: LASIK (both eyes) 52,000", {"is_current": True}),
    BM25Doc("old", "Price list: LASIK (both eyes) 45,000", {"is_current": False}),
    BM25Doc("cat", "Cataract surgery with a monofocal lens", {"is_current": True}),
    BM25Doc("kids", "Pediatric eye exam for children and squint", {"is_current": True}),
]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("How much is lasic?", ["much", "lasik"]),
        ("catract fees", ["cataract", "price"]),
        ("Glacoma screening", ["glaucoma", "screening"]),
        ("lasik ka price kitna hai", ["lasik", "price", "price"]),
        ("my son's eyes", ["child", "eye"]),
        ("Dr. Shetty's timings", ["dr", "shetty", "timing"]),
    ],
)
def test_tokenize(text: str, expected: list[str]) -> None:
    assert tokenize(text) == expected


def test_typo_query_finds_lasik() -> None:
    index = BM25Index(DOCS)
    ids = [i for i, _ in index.search("lasic cost", 3)]
    assert ids[0] in {"lasik", "price", "old"}
    assert "cat" not in ids


def test_filters_apply_before_ranking() -> None:
    index = BM25Index(DOCS)
    ids = [i for i, _ in index.search("lasik price", 5, {"is_current": True})]
    assert "old" not in ids and "price" in ids


def test_synonyms_reach_children_doc() -> None:
    index = BM25Index(DOCS)
    assert index.search("my kid's squint", 1)[0][0] == "kids"


def test_empty_and_stopword_queries() -> None:
    index = BM25Index(DOCS)
    assert index.search("", 5) == []
    assert index.search("what is the", 5) == []
    assert index.search("zebra", 5) == []


def test_empty_index() -> None:
    index = BM25Index([])
    assert len(index) == 0
    assert index.search("lasik", 5) == []


def test_vocabulary() -> None:
    assert "lasik" in BM25Index(DOCS).vocabulary
