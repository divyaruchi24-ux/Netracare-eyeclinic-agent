"""FastAPI dependencies: DB session, clock, settings."""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from typing import Annotated, Any

from fastapi import Depends, Request
from sqlalchemy.orm import Session, sessionmaker

from netracare.config import Settings
from netracare.db.session import begin_write
from netracare.domain.clock import Clock
from netracare.errors import AppError
from netracare.llm.client import LLMClient
from netracare.rag.embeddings import Embedder, Reranker
from netracare.rag.retriever import RetrievalIndex
from netracare.rag.vector_store import VectorStore
from netracare.security.rate_limit import LoginThrottle

_READ_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def get_settings_dep(request: Request) -> Settings:
    settings: Settings = request.app.state.settings
    return settings


def get_clock(request: Request) -> Clock:
    clock: Clock = request.app.state.clock
    return clock


def get_throttle(request: Request) -> LoginThrottle:
    throttle: LoginThrottle = request.app.state.login_throttle
    return throttle


def get_db(request: Request) -> Iterator[Session]:
    """One transaction per request: commit on success, rollback on error.

    Mutating requests start a write transaction up-front (SQLite BEGIN IMMEDIATE) so that
    concurrent writers are serialised instead of racing on lock upgrades. An `AppError`
    raised with `commit=True` keeps its side effects (e.g. failed-login audit rows).
    """
    factory: sessionmaker[Session] = request.app.state.session_factory
    session = factory()
    if request.method not in _READ_METHODS:
        begin_write(session)
    try:
        yield session
        session.commit()
    except AppError as exc:
        if exc.commit:
            session.commit()
        else:
            session.rollback()
        raise
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


DbSession = Annotated[Session, Depends(get_db)]
ClockDep = Annotated[Clock, Depends(get_clock)]
SettingsDep = Annotated[Settings, Depends(get_settings_dep)]
ThrottleDep = Annotated[LoginThrottle, Depends(get_throttle)]


def get_llm_client(request: Request) -> LLMClient:
    state = request.app.state
    return LLMClient(
        settings=state.settings,
        clock=state.clock,
        provider=state.llm_provider,
        registry=state.prompt_registry,
        pricing=state.pricing,
        session_factory=state.session_factory,
        sleep=getattr(state, "llm_sleep", time.sleep),
    )


LLMDep = Annotated[LLMClient, Depends(get_llm_client)]


_INDEX_LOCK = threading.Lock()


def build_rag_components(state: Any) -> tuple[Embedder, VectorStore, Reranker | None]:
    """The app's embedder / vector store / reranker: injected (tests) or the production ones
    (fastembed + Chroma), created on first use so the API starts without loading models."""
    settings: Settings = state.settings
    if getattr(state, "rag_embedder", None) is None or getattr(state, "rag_store", None) is None:
        from netracare.rag.ingest import build_components

        state.rag_embedder, state.rag_store = build_components(settings)
    if getattr(state, "rag_reranker", None) is None and settings.rag_rerank:
        from netracare.rag.embeddings import FastReranker

        state.rag_reranker = FastReranker(settings.reranker_model, settings.models_cache_dir)
    return state.rag_embedder, state.rag_store, getattr(state, "rag_reranker", None)


def get_retrieval_index(request: Request) -> RetrievalIndex:
    state = request.app.state
    index: RetrievalIndex | None = getattr(state, "retrieval_index", None)
    if index is None:
        with _INDEX_LOCK:
            index = getattr(state, "retrieval_index", None)
            if index is None:
                embedder, store, reranker = build_rag_components(state)
                index = RetrievalIndex(
                    state.session_factory, embedder, store, reranker, state.settings
                ).load()
                state.retrieval_index = index
    return index


IndexDep = Annotated[RetrievalIndex, Depends(get_retrieval_index)]
