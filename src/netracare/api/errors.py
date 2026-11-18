"""Map exceptions to a single JSON error shape: {"error": {"code", "message", "details"}}."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm.exc import StaleDataError
from starlette.exceptions import HTTPException as StarletteHTTPException

from netracare.domain.state_machine import InvalidTransitionError, TransitionForbiddenError
from netracare.errors import AppError
from netracare.log import get_logger, request_id_var

log = get_logger(__name__)


def error_response(
    status: int, code: str, message: str, details: Any = None, headers: dict[str, str] | None = None
) -> JSONResponse:
    body = {
        "error": {
            "code": code,
            "message": message,
            "details": jsonable_encoder(details),
            "request_id": request_id_var.get(),
        }
    }
    return JSONResponse(body, status_code=status, headers=headers)


def _validation_details(exc: RequestValidationError) -> list[dict[str, Any]]:
    # Drop the raw `input` echo: it may contain passwords or PII.
    return [
        {"loc": list(e.get("loc", ())), "msg": e.get("msg", ""), "type": e.get("type", "")}
        for e in exc.errors()
    ]


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def _app_error(_: Request, exc: AppError) -> JSONResponse:
        headers = {"WWW-Authenticate": "Bearer"} if exc.status == 401 else None
        return error_response(exc.status, exc.code, exc.message, exc.details, headers)

    @app.exception_handler(RequestValidationError)
    async def _validation(_: Request, exc: RequestValidationError) -> JSONResponse:
        return error_response(
            422, "validation_error", "request validation failed", _validation_details(exc)
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = {404: "not_found", 405: "method_not_allowed"}.get(exc.status_code, "http_error")
        return error_response(exc.status_code, code, str(exc.detail))

    @app.exception_handler(TransitionForbiddenError)
    async def _forbidden_transition(_: Request, exc: TransitionForbiddenError) -> JSONResponse:
        return error_response(403, "transition_forbidden", str(exc))

    @app.exception_handler(InvalidTransitionError)
    async def _invalid_transition(_: Request, exc: InvalidTransitionError) -> JSONResponse:
        return error_response(
            409, "invalid_transition", str(exc), {"status": exc.current, "event": exc.event}
        )

    @app.exception_handler(StaleDataError)
    async def _stale(_: Request, __: StaleDataError) -> JSONResponse:
        return error_response(409, "concurrent_update", "record changed by someone else; reload")

    @app.exception_handler(OperationalError)
    async def _db_busy(_: Request, exc: OperationalError) -> JSONResponse:
        if "locked" in str(exc.orig).lower():
            return error_response(
                503, "database_busy", "please retry shortly", headers={"Retry-After": "1"}
            )
        log.error("db.operational_error", error=type(exc.orig).__name__)
        return error_response(500, "internal_error", "an unexpected error occurred")

    @app.exception_handler(Exception)
    async def _unhandled(_: Request, exc: Exception) -> JSONResponse:
        log.error("unhandled_exception", error=type(exc).__name__)
        return error_response(500, "internal_error", "an unexpected error occurred")
