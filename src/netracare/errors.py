"""Application errors shared by services and the API layer.

Services raise these; `netracare.api.errors` maps them to JSON responses. `commit=True` asks
the request's DB session to commit side effects (e.g. a failed-login audit row, or a revoked
token family) before the error response is returned.
"""

from __future__ import annotations

from typing import Any, ClassVar, TypeVar

T = TypeVar("T")


class AppError(Exception):
    status: ClassVar[int] = 400

    def __init__(
        self,
        code: str,
        message: str | None = None,
        *,
        details: dict[str, Any] | None = None,
        commit: bool = False,
    ) -> None:
        super().__init__(message or code)
        self.code = code
        self.message = message or code.replace("_", " ")
        self.details = details
        self.commit = commit


class UnauthorizedError(AppError):
    status = 401


class ForbiddenError(AppError):
    status = 403


class NotFoundError(AppError):
    status = 404


class ConflictError(AppError):
    status = 409


class ValidationAppError(AppError):
    status = 422


class RateLimitedError(AppError):
    status = 429


def require(value: T | None, code: str = "not_found", message: str | None = None) -> T:
    """Return `value`, or raise NotFoundError when it is None (instead of `assert`)."""
    if value is None:
        raise NotFoundError(code, message)
    return value
