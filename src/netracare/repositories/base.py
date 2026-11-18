from __future__ import annotations

from typing import Generic, TypeVar

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from netracare.db.base import Base

T = TypeVar("T", bound=Base)


class Repository(Generic[T]):
    model: type[T]

    def __init__(self, session: Session) -> None:
        self.session = session

    def get(self, id_: str) -> T | None:
        return self.session.get(self.model, id_)

    def add(self, obj: T) -> T:
        self.session.add(obj)
        self.session.flush()
        return obj

    def list_all(self) -> list[T]:
        return list(self.session.scalars(select(self.model)))

    def count(self) -> int:
        return int(self.session.scalar(select(func.count()).select_from(self.model)) or 0)
