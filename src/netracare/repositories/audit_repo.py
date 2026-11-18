from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from netracare.db.models import AuditLog
from netracare.domain.enums import ActorType
from netracare.log import request_id_var


class AuditRepository:
    """Append-only access. Intentionally exposes no update/delete methods."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def append(
        self,
        *,
        action: str,
        entity_type: str,
        entity_id: str | None,
        actor_type: ActorType,
        actor_user_id: str | None = None,
        before: dict[str, Any] | None = None,
        after: dict[str, Any] | None = None,
        reason: str | None = None,
    ) -> AuditLog:
        entry = AuditLog(
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            actor_type=actor_type,
            actor_user_id=actor_user_id,
            before_json=before,
            after_json=after,
            reason=reason,
            request_id=request_id_var.get(),
        )
        self.session.add(entry)
        self.session.flush()
        return entry

    def for_entity(self, entity_type: str, entity_id: str) -> list[AuditLog]:
        stmt = (
            select(AuditLog)
            .where(AuditLog.entity_type == entity_type, AuditLog.entity_id == entity_id)
            .order_by(AuditLog.at_utc)
        )
        return list(self.session.scalars(stmt))


@dataclass(frozen=True)
class AuditFilters:
    entity_type: str | None = None
    entity_id: str | None = None
    actor_user_id: str | None = None
    action: str | None = None
    start: datetime | None = None
    end: datetime | None = None


def search_audit(
    session: Session, f: AuditFilters, limit: int, offset: int
) -> tuple[list[AuditLog], int]:
    """Read-only search, newest first."""
    stmt = select(AuditLog)
    if f.entity_type:
        stmt = stmt.where(AuditLog.entity_type == f.entity_type)
    if f.entity_id:
        stmt = stmt.where(AuditLog.entity_id == f.entity_id)
    if f.actor_user_id:
        stmt = stmt.where(AuditLog.actor_user_id == f.actor_user_id)
    if f.action:
        stmt = stmt.where(AuditLog.action.like(f"{f.action}%"))
    if f.start:
        stmt = stmt.where(AuditLog.at_utc >= f.start)
    if f.end:
        stmt = stmt.where(AuditLog.at_utc < f.end)
    total = int(session.scalar(select(func.count()).select_from(stmt.subquery())) or 0)
    rows = session.scalars(
        stmt.order_by(AuditLog.at_utc.desc(), AuditLog.id).limit(limit).offset(offset)
    )
    return list(rows), total
