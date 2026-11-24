"""Vector stores behind one protocol: Chroma (persistent) and an in-memory store for tests.

Only ids, vectors and filter metadata live here; chunk text stays in `kb_chunks`.
`where` filters are simple equality maps, e.g. {"is_current": True, "doc_type": "price_list"};
a list value means "any of".
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Protocol

import numpy as np

Scalar = str | int | float | bool
Where = Mapping[str, Scalar | Sequence[Scalar]]
Metadata = Mapping[str, Scalar | None]


class VectorStore(Protocol):
    def upsert(
        self, ids: Sequence[str], vectors: Sequence[Sequence[float]], metas: Sequence[Metadata]
    ) -> None: ...

    def delete(self, ids: Sequence[str]) -> None: ...

    def query(
        self, vector: Sequence[float], k: int, where: Where | None = None
    ) -> list[tuple[str, float]]:
        """Top-k `(id, cosine similarity)`, best first."""
        ...

    def ids(self) -> set[str]: ...

    def count(self) -> int: ...

    def reset(self) -> None: ...

    def get_fingerprint(self) -> str | None: ...

    def set_fingerprint(self, fingerprint: str) -> None: ...


def _matches(meta: Metadata, where: Where | None) -> bool:
    if not where:
        return True
    for key, want in where.items():
        have = meta.get(key)
        if isinstance(want, (list, tuple)):
            if have not in want:
                return False
        elif have != want:
            return False
    return True


class InMemoryVectorStore:
    def __init__(self) -> None:
        self._vectors: dict[str, np.ndarray[Any, Any]] = {}
        self._metas: dict[str, dict[str, Scalar | None]] = {}
        self._fingerprint: str | None = None

    def upsert(
        self, ids: Sequence[str], vectors: Sequence[Sequence[float]], metas: Sequence[Metadata]
    ) -> None:
        for i, v, m in zip(ids, vectors, metas, strict=True):
            self._vectors[i] = np.asarray(v, dtype=float)
            self._metas[i] = dict(m)

    def delete(self, ids: Sequence[str]) -> None:
        for i in ids:
            self._vectors.pop(i, None)
            self._metas.pop(i, None)

    def query(
        self, vector: Sequence[float], k: int, where: Where | None = None
    ) -> list[tuple[str, float]]:
        q = np.asarray(vector, dtype=float)
        scored = [
            (i, float(v @ q)) for i, v in self._vectors.items() if _matches(self._metas[i], where)
        ]
        scored.sort(key=lambda x: (-x[1], x[0]))
        return scored[:k]

    def ids(self) -> set[str]:
        return set(self._vectors)

    def count(self) -> int:
        return len(self._vectors)

    def reset(self) -> None:
        self._vectors.clear()
        self._metas.clear()
        self._fingerprint = None

    def get_fingerprint(self) -> str | None:
        return self._fingerprint

    def set_fingerprint(self, fingerprint: str) -> None:
        self._fingerprint = fingerprint


def _chroma_where(where: Where | None) -> dict[str, Any] | None:
    if not where:
        return None
    clauses: list[dict[str, Any]] = []
    for key, want in where.items():
        if isinstance(want, (list, tuple)):
            clauses.append({key: {"$in": list(want)}})
        else:
            clauses.append({key: {"$eq": want}})
    return clauses[0] if len(clauses) == 1 else {"$and": clauses}


class ChromaVectorStore:
    """Persistent Chroma collection (cosine space, telemetry off)."""

    def __init__(self, path: str, collection: str) -> None:
        import chromadb
        from chromadb.config import Settings as ChromaSettings

        self._client = chromadb.PersistentClient(
            path=path, settings=ChromaSettings(anonymized_telemetry=False, allow_reset=False)
        )
        self._name = collection
        self._col = self._open()

    def _open(self) -> Any:
        return self._client.get_or_create_collection(
            self._name, configuration={"hnsw": {"space": "cosine"}}
        )

    def upsert(
        self, ids: Sequence[str], vectors: Sequence[Sequence[float]], metas: Sequence[Metadata]
    ) -> None:
        if not ids:
            return
        clean = [{k: v for k, v in m.items() if v is not None} for m in metas]
        self._col.upsert(
            ids=list(ids), embeddings=[list(map(float, v)) for v in vectors], metadatas=clean
        )

    def delete(self, ids: Sequence[str]) -> None:
        if ids:
            self._col.delete(ids=list(ids))

    def query(
        self, vector: Sequence[float], k: int, where: Where | None = None
    ) -> list[tuple[str, float]]:
        n = self._col.count()
        if n == 0:
            return []
        res = self._col.query(
            query_embeddings=[list(map(float, vector))],
            n_results=min(k, n),
            where=_chroma_where(where),
            include=["distances"],
        )
        ids = res["ids"][0]
        distances = res["distances"][0]
        return [(i, 1.0 - float(d)) for i, d in zip(ids, distances, strict=True)]

    def ids(self) -> set[str]:
        return set(self._col.get(include=[])["ids"])

    def count(self) -> int:
        return int(self._col.count())

    def reset(self) -> None:
        self._client.delete_collection(self._name)
        self._col = self._open()

    def get_fingerprint(self) -> str | None:
        meta = self._col.metadata or {}
        value = meta.get("fingerprint")
        return str(value) if value else None

    def set_fingerprint(self, fingerprint: str) -> None:
        self._col.modify(metadata={"fingerprint": fingerprint})


def collection_name(embedding_model: str, chunker_version: str) -> str:
    """Chroma collection per (model, chunker); the content fingerprint lives in its metadata."""
    slug = "".join(ch if ch.isalnum() else "_" for ch in embedding_model.lower()).strip("_")
    return f"kb_{slug}_c{chunker_version}"[:60]
