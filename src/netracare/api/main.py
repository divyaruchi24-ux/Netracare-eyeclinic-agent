"""FastAPI application factory."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import Engine

from netracare import __version__
from netracare.api.errors import install_error_handlers
from netracare.api.middleware import RequestIdMiddleware
from netracare.api.routers import ALL_ROUTERS
from netracare.config import Settings, assert_safe_secrets, get_settings
from netracare.db.session import make_engine, make_session_factory
from netracare.domain.clock import Clock, SystemClock
from netracare.llm.client import make_provider
from netracare.llm.pricing import Pricing
from netracare.llm.prompts import PromptRegistry
from netracare.llm.providers import LLMProvider
from netracare.log import configure_logging
from netracare.observability.tracing import configure_tracing
from netracare.rag.embeddings import Embedder, Reranker
from netracare.rag.vector_store import VectorStore
from netracare.security.rate_limit import LoginThrottle, SlidingWindowLimiter


def create_app(
    settings: Settings | None = None,
    *,
    clock: Clock | None = None,
    engine: Engine | None = None,
    llm_provider: LLMProvider | None = None,
    embedder: Embedder | None = None,
    vector_store: VectorStore | None = None,
    reranker: Reranker | None = None,
    agent_checkpointer: Any = None,
) -> FastAPI:
    settings = settings or get_settings()
    assert_safe_secrets(settings)
    configure_logging(settings.log_level)
    configure_tracing(settings)

    app = FastAPI(
        title="NetraCare API",
        version=__version__,
        description="Front desk & care coordination API for the (fictional) NetraCare Eye Clinic.",
    )
    engine = engine or make_engine(settings.database_url)
    app.state.settings = settings
    app.state.clock = clock or SystemClock()
    app.state.engine = engine
    app.state.session_factory = make_session_factory(engine)
    app.state.login_throttle = LoginThrottle()
    app.state.llm_provider = llm_provider or make_provider(settings)
    app.state.prompt_registry = PromptRegistry(settings.prompts_dir).load()
    app.state.pricing = Pricing.load(settings.pricing_file)
    app.state.triage_limiter = SlidingWindowLimiter(
        settings.triage_rate_limit_per_min, timedelta(minutes=1)
    )
    # RAG: injected components (tests) or fastembed + Chroma built on first chat request.
    app.state.rag_embedder = embedder
    app.state.rag_store = vector_store
    app.state.rag_reranker = reranker
    app.state.retrieval_index = None
    # Booking agent (Phase 4): checkpoints in their own SQLite file (tests pass a memory saver)
    from netracare.agents.booking.agent import BookingAgent
    from netracare.agents.booking.checkpoint import make_checkpointer
    from netracare.agents.booking.hitl import make_decision_hook

    app.state.booking_agent = BookingAgent(agent_checkpointer or make_checkpointer(settings))
    app.state.approval_hooks = [
        make_decision_hook(app.state.booking_agent, settings, app.state.clock)
    ]
    app.state.chat_limiter = SlidingWindowLimiter(
        settings.chat_rate_limit_per_min, timedelta(minutes=1)
    )
    app.state.chat_anon_limiter = SlidingWindowLimiter(
        settings.chat_anon_rate_limit_per_min, timedelta(minutes=1)
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["X-Request-ID", "Idempotent-Replayed"],
    )
    app.add_middleware(RequestIdMiddleware)
    install_error_handlers(app)

    @app.get("/health", tags=["ops"])
    def health() -> dict[str, str]:
        return {"status": "ok", "version": __version__}

    for router in ALL_ROUTERS:
        app.include_router(router)
    return app


def app_factory() -> FastAPI:
    """Entry point for `uvicorn --factory netracare.api.main:app_factory`."""
    return create_app()
