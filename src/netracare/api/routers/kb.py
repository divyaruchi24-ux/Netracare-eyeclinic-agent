"""Knowledge base: public citation lookup, and admin status / re-index / debug search."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query, Request
from sqlalchemy import func, select

from netracare.api.deps import DbSession, IndexDep, SettingsDep, build_rag_components
from netracare.api.schemas.chat import KbChunkOut, KbDocOut, KbSearchHit, KbSearchOut, KbStatusOut
from netracare.api.security import AdminUserDep
from netracare.db.models import KbChunk, KbIndexState, KnowledgeDoc
from netracare.db.session import outside_transaction
from netracare.errors import NotFoundError
from netracare.rag.retriever import RetrievalFilters
from netracare.services.audit_service import AuditService

router = APIRouter(tags=["knowledge base"])


def _docs(db: DbSession) -> list[KbDocOut]:
    counts: dict[str, int] = {
        doc_id: n
        for doc_id, n in db.execute(
            select(KbChunk.doc_id, func.count()).group_by(KbChunk.doc_id)
        ).all()
    }
    docs = db.scalars(select(KnowledgeDoc).order_by(KnowledgeDoc.slug, KnowledgeDoc.version))
    return [
        KbDocOut(
            id=d.id,
            slug=d.slug,
            title=d.title,
            doc_type=d.doc_type.value,
            version=d.version,
            effective_date=d.effective_date,
            is_current=d.superseded_by_id is None,
            source_path=d.source_path,
            n_chunks=int(counts.get(d.id, 0)),
        )
        for d in docs
    ]


@router.get("/kb/chunks/{chunk_id}", response_model=KbChunkOut)
def get_chunk(chunk_id: str, db: DbSession) -> KbChunkOut:
    """The exact text a citation points at (KB content is public clinic information).
    Superseded chunks still resolve, labelled `is_current: false`."""
    c = db.get(KbChunk, chunk_id)
    if c is None:
        raise NotFoundError("chunk_not_found", "source not found")
    return KbChunkOut(
        id=c.id,
        slug=c.slug,
        title=c.title,
        version=c.version,
        effective_date=c.effective_date,
        doc_type=c.doc_type.value,
        heading_path=c.heading_path,
        text=c.text,
        is_current=c.is_current,
    )


@router.get("/kb/docs", response_model=list[KbDocOut])
def list_docs(db: DbSession) -> list[KbDocOut]:
    return _docs(db)


def _status(db: DbSession, vectors_ok: bool) -> KbStatusOut:
    st = db.get(KbIndexState, 1)
    return KbStatusOut(
        fingerprint=st.fingerprint if st else None,
        embedding_model=st.embedding_model if st else None,
        chunker_version=st.chunker_version if st else None,
        n_docs=st.n_docs if st else 0,
        n_chunks=st.n_chunks if st else 0,
        built_at=st.built_at if st else None,
        vectors_ok=vectors_ok,
        report=st.report if st else {},
        docs=_docs(db),
    )


@router.get("/admin/kb", response_model=KbStatusOut)
def kb_status(_: AdminUserDep, db: DbSession, index: IndexDep) -> KbStatusOut:
    return _status(db, index.vectors_ok)


@router.post("/admin/kb/reindex", response_model=KbStatusOut)
def reindex(
    request: Request,
    admin: AdminUserDep,
    db: DbSession,
    settings: SettingsDep,
    index: IndexDep,
    force: bool = False,
) -> KbStatusOut:
    """Re-run ingestion over KB_DIR, then reload the in-memory index. Ingestion manages its
    own short transactions, so the request's write lock is released while it runs."""
    from pathlib import Path

    from netracare.rag.ingest import Ingestor

    embedder, store, _ = build_rag_components(request.app.state)
    with outside_transaction(db):
        report = Ingestor(request.app.state.session_factory, embedder, store, settings).run(
            Path(settings.kb_dir), force=force
        )
        index.reload()
    AuditService(db).record(
        admin,
        "kb.reindex",
        "kb_index",
        report.fingerprint[:36],
        after={
            "chunks": report.chunks_total,
            "written": report.chunks_written,
            "deleted": report.chunks_deleted,
            "refused": len(report.refused),
        },
    )
    db.expire_all()
    return _status(db, index.vectors_ok)


@router.get("/admin/kb/search", response_model=KbSearchOut)
def kb_search(
    _: AdminUserDep,
    settings: SettingsDep,
    index: IndexDep,
    q: Annotated[str, Query(min_length=1, max_length=300)],
    include_superseded: bool = False,
) -> KbSearchOut:
    """Debug view of hybrid retrieval: per-stage ranks and scores for a query."""
    r = index.retrieve(q, RetrievalFilters(current_only=not include_superseded), k=10)
    return KbSearchOut(
        query=r.query,
        normalized_query=r.normalized_query,
        confidence=round(r.confidence, 4),
        threshold=settings.rag_min_confidence,
        answerable=r.confidence >= settings.rag_min_confidence,
        mode=r.mode,
        doctor=r.doctor.full_name if r.doctor else None,
        hits=[
            KbSearchHit(
                rank=c.rank,
                chunk_id=c.chunk.id,
                slug=c.chunk.slug,
                version=c.chunk.version,
                heading_path=c.chunk.heading_path,
                is_current=c.chunk.is_current,
                rrf_score=round(c.rrf_score, 5),
                bm25_rank=c.bm25_rank,
                vector_rank=c.vector_rank,
                cosine=None if c.cosine is None else round(c.cosine, 4),
                rerank_score=None if c.rerank_score is None else round(c.rerank_score, 3),
            )
            for c in r.chunks
        ],
        latency_ms=r.latency_ms,
    )
