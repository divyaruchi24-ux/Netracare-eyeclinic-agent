import pytest
from sqlalchemy import delete, text, update
from sqlalchemy.exc import DatabaseError
from sqlalchemy.orm import Session

from netracare.db.models import AuditLog
from netracare.domain.enums import ActorType
from netracare.log import request_id_var
from netracare.repositories.audit_repo import AuditRepository

pytestmark = pytest.mark.db


def _entry(session: Session) -> AuditLog:
    return AuditRepository(session).append(
        action="test", entity_type="thing", entity_id="1", actor_type=ActorType.SYSTEM
    )


def test_append_and_read(session: Session) -> None:
    token = request_id_var.set("req-123")
    try:
        e = _entry(session)
    finally:
        request_id_var.reset(token)
    rows = AuditRepository(session).for_entity("thing", "1")
    assert [r.id for r in rows] == [e.id]
    assert rows[0].request_id == "req-123"


def test_update_blocked(session: Session) -> None:
    e = _entry(session)
    with pytest.raises(DatabaseError, match="append-only"):
        session.execute(update(AuditLog).where(AuditLog.id == e.id).values(action="tampered"))


def test_delete_blocked(session: Session) -> None:
    e = _entry(session)
    with pytest.raises(DatabaseError, match="append-only"):
        session.execute(delete(AuditLog).where(AuditLog.id == e.id))


def test_raw_sql_delete_blocked(session: Session) -> None:
    _entry(session)
    with pytest.raises(DatabaseError, match="append-only"):
        session.execute(text("DELETE FROM audit_logs"))


def test_repository_has_no_mutators() -> None:
    public = {m for m in dir(AuditRepository) if not m.startswith("_")}
    assert not public & {"update", "delete", "remove", "edit"}
