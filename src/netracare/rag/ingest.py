"""KB ingestion: files -> knowledge_docs + kb_chunks (source of truth) + vector store.

Incremental and idempotent:
- chunk rows are diffed by a checksum over their embedded text and filter metadata, so a re-run
  with no changes writes nothing and editing one document re-embeds only its chunks;
- embedding happens *before* the write transaction, so the SQLite write lock is held for
  milliseconds, not for the model run;
- the vector store is rebuilt from scratch when its fingerprint or id set disagrees with the
  database (e.g. a crash between the DB commit and the vector upsert, or a new embedding model).

Chunks whose text trips the prompt-injection scanner are refused and reported, unless the
document sets `trusted_override: true` in its front matter (safety policy section 9).

CLI: python -m netracare.rag.ingest [--force] [--json]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session, sessionmaker

from netracare.config import Settings, get_settings
from netracare.db.base import utcnow
from netracare.db.models import KbChunk, KbIndexState, KnowledgeDoc
from netracare.db.session import session_scope
from netracare.domain.enums import DocType
from netracare.log import get_logger
from netracare.rag.chunker import CHUNKER_VERSION, chunk
from netracare.rag.embeddings import Embedder
from netracare.rag.injection import scan_text
from netracare.rag.loaders import scan
from netracare.rag.types import CHUNK_META_KEYS, IngestReport, RefusedChunk, SourceDoc
from netracare.rag.vector_store import Metadata, VectorStore
from netracare.seed.ids import seed_id
from netracare.seed.kb import REQUIRED_KEYS, link_supersession

log = get_logger(__name__)


def current_versions(docs: list[SourceDoc]) -> set[tuple[str, int]]:
    """(slug, version) of the latest document per slug: latest effective date, then version.

    Same ordering as `seed.kb.link_supersession`, computed from the files so that it is known
    before anything is written.
    """
    by_slug: dict[str, list[SourceDoc]] = defaultdict(list)
    for d in docs:
        by_slug[d.slug].append(d)
    return {
        (latest.slug, latest.version)
        for latest in (
            max(g, key=lambda d: (d.effective_date, d.version)) for g in by_slug.values()
        )
    }


def vector_meta(row: KbChunk) -> Metadata:
    return {
        "is_current": row.is_current,
        "doc_type": row.doc_type.value,
        "slug": row.slug,
        "version": row.version,
        "doctor_code": row.doctor_code,
        "specialty": row.specialty,
        "procedure": row.procedure,
    }


def _row_checksum(row: KbChunk) -> str:
    parts = [row.embed_text, str(row.is_current), row.effective_date.isoformat()]
    parts += [str(v) for v in vector_meta(row).values()]
    return hashlib.sha256("\x1f".join(parts).encode()).hexdigest()


def fingerprint(embedding_model: str, rows: list[tuple[str, str]]) -> str:
    """Identity of the whole index: model + chunker + every (chunk id, checksum)."""
    h = hashlib.sha256(f"{embedding_model}|{CHUNKER_VERSION}".encode())
    for chunk_id, checksum in sorted(rows):
        h.update(f"|{chunk_id}:{checksum}".encode())
    return h.hexdigest()


@dataclass
class _Plan:
    docs: list[SourceDoc]
    desired: dict[str, KbChunk]
    refused: list[RefusedChunk]


def _rel(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


class Ingestor:
    def __init__(
        self,
        factory: sessionmaker[Session],
        embedder: Embedder,
        store: VectorStore,
        settings: Settings | None = None,
    ) -> None:
        self._factory = factory
        self._embedder = embedder
        self._store = store
        self._settings = settings or get_settings()

    # -- planning (no writes) -------------------------------------------------------------
    def _plan(self, kb_dir: Path) -> _Plan:
        docs = scan(kb_dir)
        current = current_versions(docs)
        desired: dict[str, KbChunk] = {}
        refused: list[RefusedChunk] = []
        for doc in docs:
            doc_id = seed_id("kb", f"{doc.slug}@{doc.version}")
            is_current = (doc.slug, doc.version) in current
            for c in chunk(doc):
                reasons = scan_text(c.text)
                if reasons and not doc.trusted_override:
                    refused.append(RefusedChunk(doc.slug, c.ordinal, tuple(reasons)))
                    continue
                row = KbChunk(
                    id=c.id,
                    doc_id=doc_id,
                    ordinal=c.ordinal,
                    heading_path=c.heading_path[:512],
                    text=c.text,
                    embed_text=c.embed_text,
                    token_count=c.token_count,
                    doc_type=DocType(doc.meta["doc_type"]),
                    slug=doc.slug,
                    title=doc.title[:255],
                    version=doc.version,
                    effective_date=doc.effective_date,
                    is_current=is_current,
                    **{k: _opt(doc.meta.get(k)) for k in CHUNK_META_KEYS},
                )
                row.checksum = _row_checksum(row)
                desired[row.id] = row
        return _Plan(docs, desired, refused)

    # -- run ------------------------------------------------------------------------------
    def run(self, kb_dir: Path, *, force: bool = False, root: Path | None = None) -> IngestReport:
        started = time.perf_counter()
        root = root or Path.cwd()
        plan = self._plan(kb_dir)
        report = IngestReport(docs_seen=len(plan.docs), refused=plan.refused)

        with session_scope(self._factory) as s:
            existing = {cid: cks for cid, cks in s.execute(select(KbChunk.id, KbChunk.checksum))}
            state = s.get(KbIndexState, 1)
            old_fp = state.fingerprint if state else None

        changed = [cid for cid, row in plan.desired.items() if existing.get(cid) != row.checksum]
        stale = sorted(set(existing) - set(plan.desired))
        new_fp = fingerprint(
            self._embedder.model_id, [(cid, r.checksum) for cid, r in plan.desired.items()]
        )
        rebuild = (
            force
            or self._store.get_fingerprint() != old_fp
            or self._store.ids() != set(existing)
            or old_fp is None
        )
        to_embed = sorted(plan.desired) if rebuild else changed
        vectors = self._embedder.embed_documents([plan.desired[c].embed_text for c in to_embed])

        with session_scope(self._factory, write=True) as s:
            report.docs_changed, report.docs_removed = self._write_docs(s, plan.docs, root)
            if stale:
                s.execute(delete(KbChunk).where(KbChunk.id.in_(stale)))
            for cid in changed:
                s.merge(plan.desired[cid])
            s.flush()
            self._delete_orphan_docs(s, plan.docs)
            state = s.get(KbIndexState, 1) or KbIndexState(id=1)
            state.fingerprint = new_fp
            state.embedding_model = self._embedder.model_id
            state.chunker_version = CHUNKER_VERSION
            state.n_docs = len(plan.docs)
            state.n_chunks = len(plan.desired)
            state.built_at = utcnow()
            report.fingerprint = new_fp
            report.chunks_total = len(plan.desired)
            report.chunks_written = len(changed)
            report.chunks_deleted = len(stale)
            report.rebuilt_vectors = rebuild
            state.report = report.to_json()
            s.add(state)

        # Vector store last: a failure here leaves its fingerprint stale -> next run rebuilds.
        if rebuild:
            self._store.reset()
        else:
            self._store.delete(stale)
        self._store.upsert(to_embed, vectors, [vector_meta(plan.desired[c]) for c in to_embed])
        self._store.set_fingerprint(new_fp)

        report.duration_ms = int((time.perf_counter() - started) * 1000)
        with session_scope(self._factory, write=True) as s:
            st = s.get(KbIndexState, 1)
            if st is not None:
                st.report = report.to_json()
        log.info(
            "rag.ingest_done",
            docs=report.docs_seen,
            chunks=report.chunks_total,
            written=report.chunks_written,
            deleted=report.chunks_deleted,
            refused=len(report.refused),
            rebuilt=report.rebuilt_vectors,
            ms=report.duration_ms,
        )
        return report

    def _write_docs(self, s: Session, docs: list[SourceDoc], root: Path) -> tuple[int, int]:
        changed = 0
        for d in docs:
            doc_id = seed_id("kb", f"{d.slug}@{d.version}")
            extra = {k: v for k, v in d.meta.items() if k not in REQUIRED_KEYS}
            row = s.get(KnowledgeDoc, doc_id)
            if row is None:
                s.add(
                    KnowledgeDoc(
                        id=doc_id,
                        slug=d.slug,
                        title=d.title,
                        doc_type=DocType(d.meta["doc_type"]),
                        version=d.version,
                        effective_date=d.effective_date,
                        source_path=_rel(d.path, root),
                        checksum=d.checksum,
                        meta=_jsonable(extra),
                    )
                )
                changed += 1
            elif row.checksum != d.checksum:
                row.checksum = d.checksum
                row.title = d.title
                row.doc_type = DocType(d.meta["doc_type"])
                row.effective_date = d.effective_date
                row.source_path = _rel(d.path, root)
                row.meta = _jsonable(extra)
                changed += 1
        s.flush()
        removed = self._count_orphans(s, docs)
        link_supersession(s)
        return changed, removed

    @staticmethod
    def _orphan_ids(s: Session, docs: list[SourceDoc]) -> list[str]:
        keep = {seed_id("kb", f"{d.slug}@{d.version}") for d in docs}
        return [i for i in s.scalars(select(KnowledgeDoc.id)) if i not in keep]

    def _count_orphans(self, s: Session, docs: list[SourceDoc]) -> int:
        return len(self._orphan_ids(s, docs))

    def _delete_orphan_docs(self, s: Session, docs: list[SourceDoc]) -> None:
        orphans = self._orphan_ids(s, docs)
        if not orphans:
            return
        s.execute(delete(KbChunk).where(KbChunk.doc_id.in_(orphans)))
        s.execute(
            update(KnowledgeDoc)
            .where(KnowledgeDoc.superseded_by_id.in_(orphans))
            .values(superseded_by_id=None)
        )
        s.execute(delete(KnowledgeDoc).where(KnowledgeDoc.id.in_(orphans)))
        s.flush()
        link_supersession(s)


def _opt(value: Any) -> str | None:
    return None if value is None else str(value)


def _jsonable(meta: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = json.loads(json.dumps(meta, default=str))
    return out


def build_components(settings: Settings) -> tuple[Embedder, VectorStore]:
    """The production embedder and Chroma store for `settings`."""
    from netracare.rag.embeddings import FastEmbedder
    from netracare.rag.vector_store import ChromaVectorStore, collection_name

    embedder = FastEmbedder(settings.embedding_model, settings.models_cache_dir)
    store = ChromaVectorStore(
        settings.chroma_path, collection_name(settings.embedding_model, CHUNKER_VERSION)
    )
    return embedder, store


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m netracare.rag.ingest")
    parser.add_argument("--force", action="store_true", help="re-embed every chunk")
    parser.add_argument("--json", action="store_true", help="print the report as JSON")
    parser.add_argument("--kb-dir", default=None)
    args = parser.parse_args(argv)

    from netracare.db.session import make_engine, make_session_factory

    settings = get_settings()
    embedder, store = build_components(settings)
    factory = make_session_factory(make_engine(settings.database_url))
    report = Ingestor(factory, embedder, store, settings).run(
        Path(args.kb_dir or settings.kb_dir), force=args.force
    )
    data = report.to_json()
    if args.json:
        print(json.dumps(data, indent=2))
    else:
        print(
            f"ingested {data['docs_seen']} docs -> {data['chunks_total']} chunks "
            f"(written {data['chunks_written']}, deleted {data['chunks_deleted']}, "
            f"refused {len(data['refused'])}, vectors rebuilt: {data['rebuilt_vectors']}) "
            f"in {data['duration_ms']} ms; fingerprint {data['fingerprint'][:12]}"
        )
        for r in data["refused"]:
            print(f"  REFUSED {r['slug']}#{r['ordinal']}: {', '.join(r['reasons'])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
