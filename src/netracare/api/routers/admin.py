from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Query, status
from pydantic import AwareDatetime, EmailStr
from sqlalchemy import select

from netracare.api.deps import ClockDep, DbSession
from netracare.api.schemas.auth import Password
from netracare.api.schemas.common import Id, Page, Schema, ShortText
from netracare.api.security import AdminUserDep
from netracare.db.models import StaffUser, User
from netracare.domain.enums import ActorType, Role
from netracare.repositories.audit_repo import AuditFilters, search_audit
from netracare.services.user_service import UserService

router = APIRouter(tags=["admin"])


class UserOut(Schema):
    id: str
    email: str
    role: Role
    is_active: bool
    display_name: str | None
    doctor_id: str | None
    created_at: datetime


def _user_out(u: User, st: StaffUser | None) -> UserOut:
    return UserOut(
        id=u.id,
        email=u.email,
        role=u.role,
        is_active=u.is_active,
        display_name=st.display_name if st else None,
        doctor_id=st.doctor_id if st else None,
        created_at=u.created_at,
    )


class StaffCreate(Schema):
    email: EmailStr
    display_name: ShortText
    role: Role
    temp_password: Password
    doctor_id: Id | None = None


class UserUpdate(Schema):
    role: Role | None = None
    is_active: bool | None = None


class AuditOut(Schema):
    id: str
    at_utc: datetime
    actor_user_id: str | None
    actor_type: ActorType
    action: str
    entity_type: str
    entity_id: str | None
    before_json: dict[str, Any] | None
    after_json: dict[str, Any] | None
    request_id: str | None
    reason: str | None


@router.get("/users")
def list_users(
    user: AdminUserDep,
    db: DbSession,
    clock: ClockDep,
    role: Role | None = None,
    q: Annotated[str | None, Query(max_length=64)] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> Page[UserOut]:
    rows, total = UserService(db, clock).list_users(role, q, limit, offset)
    items = [_user_out(u, st) for u, st in rows]
    return Page[UserOut](items=items, total=total, limit=limit, offset=offset)


@router.post("/users", status_code=status.HTTP_201_CREATED)
def create_user(body: StaffCreate, user: AdminUserDep, db: DbSession, clock: ClockDep) -> UserOut:
    u, st = UserService(db, clock).create_staff(
        user,
        email=str(body.email),
        display_name=body.display_name,
        role=body.role,
        temp_password=body.temp_password,
        doctor_id=body.doctor_id,
    )
    return _user_out(u, st)


@router.patch("/users/{user_id}")
def update_user(
    user_id: str, body: UserUpdate, user: AdminUserDep, db: DbSession, clock: ClockDep
) -> UserOut:
    u = UserService(db, clock).update_user(user, user_id, role=body.role, is_active=body.is_active)
    staff = db.scalar(select(StaffUser).where(StaffUser.user_id == u.id))
    return _user_out(u, staff)


@router.get("/audit-logs")
def audit_logs(
    user: AdminUserDep,
    db: DbSession,
    entity_type: Annotated[str | None, Query(max_length=64)] = None,
    entity_id: Annotated[str | None, Query(max_length=36)] = None,
    actor_user_id: Annotated[str | None, Query(max_length=36)] = None,
    action: Annotated[str | None, Query(max_length=64)] = None,
    start: Annotated[AwareDatetime | None, Query(alias="from")] = None,
    end: Annotated[AwareDatetime | None, Query(alias="to")] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> Page[AuditOut]:
    filters = AuditFilters(entity_type, entity_id, actor_user_id, action, start, end)
    rows, total = search_audit(db, filters, limit, offset)
    items = [AuditOut.model_validate(r) for r in rows]
    return Page[AuditOut](items=items, total=total, limit=limit, offset=offset)
