"""Hybrid retrieval: BM25 + vectors, fused with Reciprocal Rank Fusion, optional rerank.

    query -> normalise (typo map, fuzzy doctor name) -> filters (current docs only by default)
          -> BM25 top-N ┐
          -> vector top-N ┴-> RRF (+ boost lists) -> [rerank top-M] -> top-k + confidence

Superseded documents are excluded *at retrieval time* (`current_only=True`), so the latest
effective date always wins; the model never has to choose between two price lists.

Confidence (used to refuse below `rag_min_confidence`, docs/phase-3/understanding.md 3.6):
sigmoid(top rerank score) when rerank is on, else the best cosine among the vector hits.
If the vector store is stale or unavailable the index serves BM25 only; confidence then
comes from the reranker if enabled, else 0.0 (every question is refused: safe, not wrong).
"""

from __future__ import annotations

import re
import threading
import time
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import date
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from netracare.config import Settings
from netracare.db.models import Doctor, KbChunk, KbIndexState
from netracare.db.session import session_scope
from netracare.log import get_logger
from netracare.observability.tracing import traced
from netracare.rag.bm25 import TYPO_MAP, BM25Doc, BM25Index
from netracare.rag.doctor_names import DoctorMatch, DoctorName, match_doctor
from netracare.rag.embeddings import Embedder, Reranker, sigmoid
from netracare.rag.vector_store import Scalar, VectorStore

log = get_logger(__name__)


class IndexStaleError(RuntimeError):
    """The vector store does not match `kb_chunks` (re-run ingestion)."""


@dataclass(frozen=True)
class ChunkRecord:
    """A detached copy of a `kb_chunks` row (safe to share across threads and requests)."""

    id: str
    slug: str
    title: str
    version: int
    effective_date: date
    doc_type: str
    heading_path: str
    text: str
    embed_text: str
    is_current: bool
    doctor_code: str | None
    specialty: str | None
    procedure: str | None

    @property
    def meta(self) -> dict[str, Scalar | None]:
        return {
            "is_current": self.is_current,
            "doc_type": self.doc_type,
            "slug": self.slug,
            "version": self.version,
            "doctor_code": self.doctor_code,
            "specialty": self.specialty,
            "procedure": self.procedure,
        }

    @classmethod
    def from_row(cls, r: KbChunk) -> ChunkRecord:
        return cls(
            id=r.id,
            slug=r.slug,
            title=r.title,
            version=r.version,
            effective_date=r.effective_date,
            doc_type=r.doc_type.value,
            heading_path=r.heading_path,
            text=r.text,
            embed_text=r.embed_text,
            is_current=r.is_current,
            doctor_code=r.doctor_code,
            specialty=r.specialty,
            procedure=r.procedure,
        )


@dataclass(frozen=True)
class RetrievalFilters:
    current_only: bool = True
    doc_types: tuple[str, ...] | None = None  # hard filter
    doctor_code: str | None = None  # hard filter
    specialty: str | None = None  # hard filter
    boost_doc_types: tuple[str, ...] = ()  # soft: an extra RRF list (pricing -> price_list)

    def where(self) -> dict[str, Scalar | Sequence[Scalar]]:
        w: dict[str, Scalar | Sequence[Scalar]] = {}
        if self.current_only:
            w["is_current"] = True
        if self.doc_types:
            w["doc_type"] = list(self.doc_types)
        if self.doctor_code:
            w["doctor_code"] = self.doctor_code
        if self.specialty:
            w["specialty"] = self.specialty
        return w


@dataclass(frozen=True)
class RetrievedChunk:
    chunk: ChunkRecord
    rank: int
    rrf_score: float
    bm25_rank: int | None = None
    vector_rank: int | None = None
    cosine: float | None = None
    rerank_score: float | None = None


@dataclass
class RetrievalResult:
    query: str
    normalized_query: str
    chunks: list[RetrievedChunk]
    confidence: float
    mode: str  # "hybrid" | "bm25_only"
    doctor: DoctorMatch | None = None
    stages: dict[str, list[tuple[str, float]]] = field(default_factory=dict)
    latency_ms: int = 0

    @property
    def top(self) -> RetrievedChunk | None:
        return self.chunks[0] if self.chunks else None

    def summary(self) -> dict[str, Any]:
        """Trace/log payload: ids, ranks and scores only (never chunk text or the query)."""
        return {
            "mode": self.mode,
            "confidence": round(self.confidence, 4),
            "doctor": self.doctor.code if self.doctor else None,
            "top": [
                {
                    "id": c.chunk.id,
                    "slug": c.chunk.slug,
                    "rrf": round(c.rrf_score, 5),
                    "bm25_rank": c.bm25_rank,
                    "vector_rank": c.vector_rank,
                    "rerank": None if c.rerank_score is None else round(c.rerank_score, 3),
                }
                for c in self.chunks
            ],
            "stages": {k: [i for i, _ in v[:10]] for k, v in self.stages.items()},
            "latency_ms": self.latency_ms,
        }


def rrf(rankings: Sequence[Sequence[str]], k: int = 60) -> list[tuple[str, float]]:
    """Reciprocal Rank Fusion: score(d) = sum over lists of 1 / (k + rank), rank from 1.

    Ties break by first appearance across the lists, then by id, so results are stable.
    """
    scores: dict[str, float] = defaultdict(float)
    first_seen: dict[str, tuple[int, int]] = {}
    for li, ranking in enumerate(rankings):
        for rank, doc_id in enumerate(ranking, start=1):
            scores[doc_id] += 1.0 / (k + rank)
            first_seen.setdefault(doc_id, (rank, li))
    return sorted(scores.items(), key=lambda x: (-x[1], first_seen[x[0]], x[0]))


_WORD = re.compile(r"[A-Za-z]+")


def normalize_query(text: str, doctor: DoctorMatch | None) -> str:
    """Fix known typos word by word and spell a fuzzy-matched doctor's name correctly."""
    out = _WORD.sub(lambda m: TYPO_MAP.get(m.group(0).lower(), m.group(0)), text.strip())
    if doctor and doctor.score < 1.0:
        surname = doctor.full_name.split()[-1]
        out = re.sub(rf"\b{re.escape(doctor.matched)}\b", surname, out, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", out)


class RetrievalIndex:
    """App-scoped retrieval index: chunk records + BM25 in memory, vectors in the store.

    `load()` (or `reload()` after re-ingestion) reads `kb_chunks`; it is cheap (~100 rows).
    Thread-safe: a reload swaps in new immutable state.
    """

    def __init__(
        self,
        factory: sessionmaker[Session],
        embedder: Embedder,
        store: VectorStore,
        reranker: Reranker | None,
        settings: Settings,
    ) -> None:
        self._factory = factory
        self._embedder = embedder
        self._store = store
        self._reranker = reranker
        self._settings = settings
        self._lock = threading.Lock()
        self._records: dict[str, ChunkRecord] = {}
        self._bm25 = BM25Index([])
        self._doctors: list[DoctorName] = []
        self._vectors_ok = False
        self._loaded = False
        self.fingerprint: str | None = None

    # -- state --------------------------------------------------------------------------
    def load(self) -> RetrievalIndex:
        with session_scope(self._factory) as s:
            rows = [ChunkRecord.from_row(r) for r in s.scalars(select(KbChunk))]
            state = s.get(KbIndexState, 1)
            doctors = [DoctorName(d.code, d.full_name) for d in s.scalars(select(Doctor))]
        fingerprint = state.fingerprint if state else None
        try:
            vectors_ok = (
                fingerprint is not None
                and self._store.get_fingerprint() == fingerprint
                and self._store.ids() == {r.id for r in rows}
            )
        except Exception as exc:  # a broken store must not take chat down
            log.warning("rag.vector_store_unavailable", error=type(exc).__name__)
            vectors_ok = False
        if rows and not vectors_ok:
            log.warning(
                "rag.index_stale", chunks=len(rows), hint="run python -m netracare.rag.ingest"
            )
        bm25 = BM25Index([BM25Doc(r.id, r.embed_text, r.meta) for r in rows])
        with self._lock:
            self._records = {r.id: r for r in rows}
            self._bm25 = bm25
            self._doctors = doctors
            self._vectors_ok = vectors_ok
            self.fingerprint = fingerprint
            self._loaded = True
        return self

    reload = load

    def ensure_loaded(self) -> RetrievalIndex:
        if not self._loaded:
            self.load()
        return self

    @property
    def vectors_ok(self) -> bool:
        return self._vectors_ok

    @property
    def doctors(self) -> list[DoctorName]:
        return list(self._doctors)

    def require_fresh(self) -> None:
        if not self.ensure_loaded()._vectors_ok:
            raise IndexStaleError("vector store does not match kb_chunks; run ingestion")

    def chunk(self, chunk_id: str) -> ChunkRecord | None:
        return self.ensure_loaded()._records.get(chunk_id)

    def __len__(self) -> int:
        return len(self.ensure_loaded()._records)

    # -- retrieval ----------------------------------------------------------------------
    @traced(
        "rag.retrieve",
        tags=["rag"],
        inputs=lambda self, query, filters=None, k=None: {
            "query_chars": len(query),
            "filters": (filters or RetrievalFilters()).where(),
            "k": k,
        },
        outputs=lambda r: r.summary(),
    )
    def retrieve(
        self, query: str, filters: RetrievalFilters | None = None, k: int | None = None
    ) -> RetrievalResult:
        started = time.perf_counter()
        self.ensure_loaded()
        s = self._settings
        filters = filters or RetrievalFilters()
        k = k or s.rag_top_k
        doctor = match_doctor(query, self._doctors)
        normalized = normalize_query(query, doctor)
        where = filters.where()

        bm25_hits = self._bm25.search(normalized, s.rag_bm25_k, where)
        vector_hits: list[tuple[str, float]] = []
        mode = "bm25_only"
        if self._vectors_ok and normalized:
            try:
                qv = self._embedder.embed_query(normalized)
                vector_hits = self._store.query(qv, s.rag_vector_k, where)
                mode = "hybrid"
            except Exception as exc:
                log.warning("rag.vector_query_failed", error=type(exc).__name__)

        rankings: list[list[str]] = [[i for i, _ in bm25_hits], [i for i, _ in vector_hits]]
        candidate_ids = {i for r in rankings for i in r}
        if doctor:  # soft boost: chunks about the named doctor
            rankings.append(self._boost_ids(candidate_ids, where, doctor_code=doctor.code))
        if filters.boost_doc_types:
            rankings.append(
                self._boost_ids(candidate_ids, where, doc_types=filters.boost_doc_types)
            )
        fused = [(i, sc) for i, sc in rrf(rankings, s.rag_rrf_k) if i in self._records]

        bm25_rank = {i: n for n, (i, _) in enumerate(bm25_hits, start=1)}
        vec_rank = {i: n for n, (i, _) in enumerate(vector_hits, start=1)}
        cosines = dict(vector_hits)
        results = [
            RetrievedChunk(
                chunk=self._records[i],
                rank=n,
                rrf_score=sc,
                bm25_rank=bm25_rank.get(i),
                vector_rank=vec_rank.get(i),
                cosine=cosines.get(i),
            )
            for n, (i, sc) in enumerate(fused, start=1)
        ]

        stages: dict[str, list[tuple[str, float]]] = {
            "bm25": bm25_hits,
            "vector": vector_hits,
            "fused": fused[: s.rag_rerank_k],
        }
        confidence = 0.0
        if self._reranker is not None and s.rag_rerank and results:
            head = results[: s.rag_rerank_k]
            try:
                scores = self._reranker.score(normalized, [c.chunk.embed_text for c in head])
                order = sorted(zip(head, scores, strict=True), key=lambda x: (-x[1], x[0].rank))
                results = [
                    replace(c, rank=n, rerank_score=sc) for n, (c, sc) in enumerate(order, start=1)
                ] + results[len(head) :]
                stages["rerank"] = [(c.chunk.id, sc) for c, sc in order]
                confidence = sigmoid(order[0][1])
            except Exception as exc:
                log.warning("rag.rerank_failed", error=type(exc).__name__)
                confidence = max(cosines.values(), default=0.0)
        else:
            confidence = max(cosines.values(), default=0.0)

        return RetrievalResult(
            query=query,
            normalized_query=normalized,
            chunks=results[:k],
            confidence=float(confidence),
            mode=mode,
            doctor=doctor,
            stages=stages,
            latency_ms=int((time.perf_counter() - started) * 1000),
        )

    def _boost_ids(
        self,
        candidates: set[str],
        where: Mapping[str, Scalar | Sequence[Scalar]],
        *,
        doctor_code: str | None = None,
        doc_types: Sequence[str] | None = None,
    ) -> list[str]:
        """Records matching the filters plus the boost condition, ordered by doc then position.

        Restricted to records the base retrievers already found *or* that belong to the boosted
        set, so a doctor's bio surfaces even when the query only names the doctor.
        """
        from netracare.rag.vector_store import _matches

        picked = [
            r
            for r in self._records.values()
            if _matches(r.meta, where)
            and (doctor_code is None or r.doctor_code == doctor_code)
            and (doc_types is None or r.doc_type in doc_types)
        ]
        # Doctor bios first for a doctor boost; otherwise keep base-retriever candidates first.
        picked.sort(
            key=lambda r: (
                0 if (doctor_code and r.doc_type == "doctor_bio") else 1,
                0 if r.id in candidates else 1,
                r.slug,
                r.id,
            )
        )
        return [r.id for r in picked]
