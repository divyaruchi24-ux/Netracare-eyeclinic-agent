"""Idempotency keys: a retried request replays the stored response instead of re-executing."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from netracare.db.models import Appointment, IdempotencyRecord
from netracare.domain.clock import Clock
from netracare.errors import ValidationAppError

KEY_TTL = timedelta(hours=24)
_KEY_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")


@dataclass(frozen=True)
class StoredResponse:
    status_code: int
    body: dict[str, Any]


def validate_key(key: str) -> str:
    if not _KEY_RE.fullmatch(key):
        raise ValidationAppError(
            "invalid_idempotency_key", "Idempotency-Key must be 8-64 chars of [A-Za-z0-9_-]"
        )
    return key


def request_hash(payload: Any) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()


def scoped_key(user_id: str, key: str) -> str:
    """Globally unique, fixed-length form stored on the appointment row (DB-level guard)."""
    return hashlib.sha256(f"{user_id}:{key}".encode()).hexdigest()


class IdempotencyService:
    def __init__(self, session: Session, clock: Clock) -> None:
        self.s = session
        self.clock = clock

    def lookup(
        self, user_id: str, key: str, method: str, path: str, body_hash: str
    ) -> StoredResponse | None:
        rec = self.s.scalar(
            select(IdempotencyRecord).where(
                IdempotencyRecord.user_id == user_id, IdempotencyRecord.key == key
            )
        )
        if rec is None:
            return None
        if self.clock.now_utc() - rec.created_at >= KEY_TTL:
            self._expire(rec)
            return None
        if (rec.method, rec.path, rec.request_hash) != (method, path, body_hash):
            raise ValidationAppError(
                "idempotency_key_reuse",
                "this Idempotency-Key was already used for a different request",
            )
        return StoredResponse(rec.status_code, rec.response_json)

    def store(
        self,
        user_id: str,
        key: str,
        method: str,
        path: str,
        body_hash: str,
        status_code: int,
        body: dict[str, Any],
    ) -> None:
        self.s.add(
            IdempotencyRecord(
                user_id=user_id,
                key=key,
                method=method,
                path=path,
                request_hash=body_hash,
                status_code=status_code,
                response_json=body,
                created_at=self.clock.now_utc(),
            )
        )
        self.s.flush()

    def _expire(self, rec: IdempotencyRecord) -> None:
        # Release the DB-level guard on the appointment too, so the key becomes reusable.
        self.s.execute(
            update(Appointment)
            .where(Appointment.idempotency_key == scoped_key(rec.user_id, rec.key))
            .values(idempotency_key=None)
        )
        self.s.delete(rec)
        self.s.flush()

    def purge_expired(self) -> int:
        cutoff = self.clock.now_utc() - KEY_TTL
        expired = list(
            self.s.scalars(select(IdempotencyRecord).where(IdempotencyRecord.created_at < cutoff))
        )
        for rec in expired:
            self._expire(rec)
        return len(expired)
