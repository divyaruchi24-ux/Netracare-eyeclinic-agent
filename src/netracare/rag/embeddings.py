"""Embedding and reranking models behind small protocols.

`FastEmbedder` / `FastReranker` run the ONNX models from fastembed (no torch). Tests use the
deterministic `HashingEmbedder` / `OverlapReranker`, which need no download.
"""

from __future__ import annotations

import hashlib
import math
import re
import threading
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Protocol

import numpy as np

from netracare.log import get_logger

log = get_logger(__name__)

# BAAI's recommended instruction for short retrieval queries with bge-*-en-v1.5 models
# (documents are embedded without it). fastembed does not add it itself.
BGE_QUERY_PREFIX = "Represent this sentence for searching relevant passages: "

Vector = list[float]


class Embedder(Protocol):
    model_id: str
    dim: int

    def embed_documents(self, texts: Sequence[str]) -> list[Vector]: ...

    def embed_query(self, text: str) -> Vector: ...


class Reranker(Protocol):
    model_id: str

    def score(self, query: str, texts: Sequence[str]) -> list[float]: ...


def _normalise(v: np.ndarray[Any, Any]) -> Vector:
    norm = float(np.linalg.norm(v))
    return [float(x) for x in (v / norm if norm else v)]


_LOCK = threading.Lock()
_MODELS: dict[tuple[str, str, str], Any] = {}


def _load(kind: str, model_id: str, cache_dir: str) -> Any:
    """One shared model instance per (kind, model, cache dir); loading is slow-ish (~2 s)."""
    key = (kind, model_id, cache_dir)
    with _LOCK:
        if key not in _MODELS:
            Path(cache_dir).mkdir(parents=True, exist_ok=True)
            if kind == "embed":
                from fastembed import TextEmbedding

                _MODELS[key] = TextEmbedding(model_id, cache_dir=cache_dir)
            else:
                from fastembed.rerank.cross_encoder import TextCrossEncoder

                _MODELS[key] = TextCrossEncoder(model_id, cache_dir=cache_dir)
            log.info("rag.model_loaded", kind=kind, model=model_id)
        return _MODELS[key]


class FastEmbedder:
    def __init__(self, model_id: str, cache_dir: str, dim: int = 384) -> None:
        self.model_id = model_id
        self.dim = dim
        self._cache_dir = cache_dir

    @property
    def _model(self) -> Any:
        return _load("embed", self.model_id, self._cache_dir)

    def embed_documents(self, texts: Sequence[str]) -> list[Vector]:
        if not texts:
            return []
        return [_normalise(np.asarray(v)) for v in self._model.embed(list(texts))]

    def embed_query(self, text: str) -> Vector:
        prefix = BGE_QUERY_PREFIX if "bge-" in self.model_id and "-en" in self.model_id else ""
        return _normalise(np.asarray(next(iter(self._model.embed([prefix + text])))))


class FastReranker:
    def __init__(self, model_id: str, cache_dir: str) -> None:
        self.model_id = model_id
        self._cache_dir = cache_dir

    def score(self, query: str, texts: Sequence[str]) -> list[float]:
        if not texts:
            return []
        model = _load("rerank", self.model_id, self._cache_dir)
        return [float(s) for s in model.rerank(query, list(texts))]


_WORD = re.compile(r"[a-z0-9]+")


def _features(text: str) -> list[str]:
    words = _WORD.findall(text.lower())
    grams = [w[i : i + 3] for w in words if len(w) > 3 for i in range(len(w) - 2)]
    return words + grams


class HashingEmbedder:
    """Deterministic bag-of-words + character-trigram hashing embedder (tests, no download)."""

    def __init__(self, dim: int = 256) -> None:
        self.model_id = f"hashing-{dim}"
        self.dim = dim

    def _embed(self, text: str) -> Vector:
        v = np.zeros(self.dim)
        for feature in _features(text):
            h = int(hashlib.md5(feature.encode(), usedforsecurity=False).hexdigest(), 16)
            v[h % self.dim] += 1.0 if (h >> 64) & 1 else -1.0
        return _normalise(v)

    def embed_documents(self, texts: Sequence[str]) -> list[Vector]:
        return [self._embed(t) for t in texts]

    def embed_query(self, text: str) -> Vector:
        return self._embed(text)


class OverlapReranker:
    """Test reranker: logit-like score from query/document word overlap."""

    model_id = "overlap"

    def score(self, query: str, texts: Sequence[str]) -> list[float]:
        q = set(_WORD.findall(query.lower()))
        out: list[float] = []
        for t in texts:
            d = set(_WORD.findall(t.lower()))
            overlap = len(q & d) / max(len(q), 1)
            out.append(10.0 * overlap - 5.0)
        return out


def sigmoid(x: float) -> float:
    return 1.0 / (1.0 + math.exp(-x))
