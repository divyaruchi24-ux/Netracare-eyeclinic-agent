"""FastAPI application factory."""

from __future__ import annotations

from fastapi import FastAPI

from netracare import __version__
from netracare.config import get_settings
from netracare.log import configure_logging


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings.log_level)
    app = FastAPI(title="NetraCare API", version=__version__)

    @app.get("/health", tags=["ops"])
    def health() -> dict[str, str]:
        return {"status": "ok", "version": __version__}

    return app


app = create_app()
