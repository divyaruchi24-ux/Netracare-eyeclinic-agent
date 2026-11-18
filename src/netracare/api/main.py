"""FastAPI application factory."""

from __future__ import annotations

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
from netracare.log import configure_logging
from netracare.observability.tracing import configure_tracing
from netracare.security.rate_limit import LoginThrottle


def create_app(
    settings: Settings | None = None,
    *,
    clock: Clock | None = None,
    engine: Engine | None = None,
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
