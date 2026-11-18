from __future__ import annotations

from datetime import datetime
from typing import Annotated, Generic, TypeVar
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, StringConstraints

T = TypeVar("T")

# Free text from users: stripped, bounded, non-empty.
ShortText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=128)]
Note = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]
Id = Annotated[str, StringConstraints(min_length=1, max_length=36)]


class Schema(BaseModel):
    model_config = ConfigDict(from_attributes=True, extra="forbid")


class Page(Schema, Generic[T]):
    items: list[T]
    total: int
    limit: int
    offset: int


class ErrorDetail(Schema):
    code: str
    message: str
    details: object | None = None
    request_id: str | None = None


class ErrorBody(Schema):
    error: ErrorDetail


def local_iso(dt: datetime, tz: ZoneInfo) -> str:
    return dt.astimezone(tz).isoformat()
