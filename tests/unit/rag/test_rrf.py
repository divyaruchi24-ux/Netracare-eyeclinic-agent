"""Reciprocal Rank Fusion properties (randomised with fixed seeds; no hypothesis dependency)."""

import random

import pytest

from netracare.rag.retriever import rrf


def test_single_list_keeps_its_order() -> None:
    assert [i for i, _ in rrf([["a", "b", "c"]])] == ["a", "b", "c"]


def test_score_formula() -> None:
    fused = dict(rrf([["a", "b"], ["b"]], k=60))
    assert fused["a"] == pytest.approx(1 / 61)
    assert fused["b"] == pytest.approx(1 / 62 + 1 / 61)


def test_agreement_beats_single_top_rank() -> None:
    # "b" is second in both lists; "a" and "c" are first in one list each.
    assert rrf([["a", "b"], ["c", "b"]])[0][0] == "b"


def test_ties_break_by_first_appearance_then_id() -> None:
    assert [i for i, _ in rrf([["x"], ["y"]])] == ["x", "y"]
    assert [i for i, _ in rrf([["y"], ["x"]])] == ["y", "x"]


def test_empty_inputs() -> None:
    assert rrf([]) == []
    assert rrf([[], []]) == []


@pytest.mark.parametrize("seed", range(25))
def test_properties_on_random_rankings(seed: int) -> None:
    rng = random.Random(seed)
    ids = [f"d{i}" for i in range(12)]
    lists = [rng.sample(ids, rng.randint(0, len(ids))) for _ in range(rng.randint(1, 4))]
    fused = rrf(lists)
    # Every input id appears exactly once; scores are positive and sorted descending.
    assert sorted(i for i, _ in fused) == sorted({i for li in lists for i in li})
    scores = [s for _, s in fused]
    assert all(s > 0 for s in scores) and scores == sorted(scores, reverse=True)
    # Monotonic: moving an id up in one list never lowers its fused score.
    target = next((li for li in lists if len(li) > 1), None)
    if target is not None:
        doc = target[-1]
        before = dict(fused)[doc]
        promoted = [doc, *[x for x in target if x != doc]]
        after = dict(rrf([promoted if li is target else li for li in lists]))[doc]
        assert after >= before
    # Deterministic.
    assert rrf(lists) == fused
