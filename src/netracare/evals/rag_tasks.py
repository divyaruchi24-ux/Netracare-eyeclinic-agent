"""Eval tasks for retrieval (Phase 3).

`rag_retrieval` runs the real hybrid retriever (fastembed models, downloaded once into
MODELS_CACHE_DIR) over the real `kb/`, indexed into a throwaway SQLite DB + in-memory vector
store. It calls no LLM, so it is an offline CI gate (CI caches the model files).
"""

from __future__ import annotations

import tempfile
from dataclasses import dataclass
from datetime import date
from functools import lru_cache
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session, sessionmaker

from netracare.config import Settings, get_settings
from netracare.rag.embeddings import Embedder, Reranker
from netracare.rag.retriever import RetrievalFilters, RetrievalIndex
from netracare.rag.vector_store import InMemoryVectorStore

_EVAL_TODAY = date(2026, 11, 10)


@dataclass(frozen=True)
class RagEvalBase:
    factory: sessionmaker[Session]
    embedder: Embedder
    store: InMemoryVectorStore
    reranker: Reranker


@lru_cache(maxsize=1)
def rag_eval_base(root: str = ".") -> RagEvalBase:
    """Seed a temp DB (doctors are needed for fuzzy name matching) and ingest `kb/`."""
    from netracare.db.base import Base
    from netracare.db.session import make_engine, make_session_factory
    from netracare.rag.embeddings import FastEmbedder, FastReranker
    from netracare.rag.ingest import Ingestor
    from netracare.seed.runner import Seeder

    settings = get_settings()
    base = Path(root)
    tmp = Path(tempfile.mkdtemp(prefix="netracare-rag-eval-"))
    engine = make_engine(f"sqlite:///{(tmp / 'eval.db').as_posix()}")
    Base.metadata.create_all(engine)
    factory = make_session_factory(engine)
    with factory() as s:
        Seeder(
            s,
            data_dir=base / "seed" / "data",
            kb_dir=base / settings.kb_dir,
            root=base,
            today=_EVAL_TODAY,
            tz=ZoneInfo(settings.clinic_timezone),
            password=settings.seed_default_password.get_secret_value(),
        ).run()
        s.commit()
    embedder = FastEmbedder(settings.embedding_model, settings.models_cache_dir)
    store = InMemoryVectorStore()
    Ingestor(factory, embedder, store, settings).run(base / settings.kb_dir, root=base)
    reranker = FastReranker(settings.reranker_model, settings.models_cache_dir)
    return RagEvalBase(factory, embedder, store, reranker)


def make_index(settings: Settings, root: str = ".") -> RetrievalIndex:
    b = rag_eval_base(root)
    return RetrievalIndex(b.factory, b.embedder, b.store, b.reranker, settings).load()


@lru_cache(maxsize=1)
def _default_index() -> RetrievalIndex:
    return make_index(get_settings())


def retrieval_output(
    index: RetrievalIndex, inp: dict[str, Any], threshold: float
) -> dict[str, Any]:
    f = inp.get("filters") or {}
    filters = RetrievalFilters(
        doc_types=tuple(f["doc_types"]) if f.get("doc_types") else None,
        boost_doc_types=tuple(f.get("boost_doc_types") or ()),
    )
    r = index.retrieve(str(inp["query"]), filters)
    return {
        "slugs": [c.chunk.slug for c in r.chunks],
        "chunk_ids": [c.chunk.id for c in r.chunks],
        "all_current": all(c.chunk.is_current for c in r.chunks),
        "confidence": round(r.confidence, 4),
        "max_cosine": round(max((c.cosine or 0.0 for c in r.chunks), default=0.0), 4),
        "answerable": r.confidence >= threshold,
        "mode": r.mode,
    }


def rag_retrieval_task(inp: dict[str, Any]) -> dict[str, Any]:
    return retrieval_output(_default_index(), inp, get_settings().rag_min_confidence)


def rag_answer_live_task(inp: dict[str, Any]) -> dict[str, Any]:  # pragma: no cover - live
    """The real ChatService (router -> retrieval -> cited answer) on Groq, anonymous session."""
    from netracare.db.models import ChatMessage
    from netracare.domain.clock import SystemClock
    from netracare.domain.enums import ChatRole
    from netracare.evals.tasks import live_llm
    from netracare.services.chat_service import ChatService

    settings = get_settings()
    base = rag_eval_base()
    index = _default_index()
    with base.factory() as s:
        svc = ChatService(s, SystemClock(), settings, live_llm(), index)
        chat, token = svc.create_session(None)
        lines = str(inp.get("history") or "").splitlines()
        for seq, line in enumerate(lines, start=1):
            role, _, text = line.partition(":")
            s.add(
                ChatMessage(
                    session_id=chat.id,
                    seq=seq,
                    role=ChatRole.USER if role.strip() == "user" else ChatRole.ASSISTANT,
                    text=text.strip(),
                )
            )
        s.flush()
        turn = svc.post_message(chat.id, str(inp["query"]), actor=None, anon_token=token)
        s.commit()
    cited = [index.chunk(c.chunk_id) for c in turn.citations]
    return {
        "reply": turn.reply,
        "mode": turn.mode,
        "intent": turn.intent.value,
        "citations": [
            {
                "chunk_id": c.chunk_id,
                "slug": c.slug,
                "version": c.version,
                "is_current": c.is_current,
            }
            for c in turn.citations
        ],
        "cited_text": "\n".join(r.text for r in cited if r is not None),
        "all_resolvable": all(r is not None for r in cited),
        "flags": turn.flags,
        "cost_usd": turn.cost_usd,
    }
