"""Thin wrapper that records who did what (append-only)."""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from netracare.db.models import AuditLog
from netracare.domain.actor import CurrentUser
from netracare.domain.enums import ActorType
from netracare.repositories.audit_repo import AuditRepository


class AuditService:
    def __init__(self, session: Session) -> None:
        self.repo = AuditRepository(session)

    def record(
        self,
        actor: CurrentUser | str | None,
        action: str,
        entity_type: str,
        entity_id: str | None,
        *,
        before: dict[str, Any] | None = None,
        after: dict[str, Any] | None = None,
        reason: str | None = None,
    ) -> AuditLog:
        """`actor` is the acting user (or their user id); None means the system."""
        return self.repo.append(
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            actor_type=ActorType.USER if actor else ActorType.SYSTEM,
            actor_user_id=actor if isinstance(actor, str) or actor is None else actor.id,
            before=before,
            after=after,
            reason=reason,
        )
