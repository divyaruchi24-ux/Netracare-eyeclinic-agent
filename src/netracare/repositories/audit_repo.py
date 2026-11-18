from __future__ import annotations

from typing import Any

from sqlalchemy import select
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
