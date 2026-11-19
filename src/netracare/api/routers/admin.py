from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Query, status
from pydantic import AwareDatetime, EmailStr
from sqlalchemy import select

from netracare.api.deps import ClockDep, DbSession, SettingsDep
from netracare.api.schemas.auth import Password
from netracare.api.schemas.common import Id, Page, Schema, ShortText
from netracare.api.security import AdminUserDep
from netracare.db.models import StaffUser, User
from netracare.domain.enums import ActorType, Role
from netracare.llm.usage import usage_report
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


class LLMCallOut(Schema):
    id: str
    at_utc: datetime
    purpose: str
    prompt_id: str
    prompt_version: int
    model: str
    input_tokens: int
    output_tokens: int
    cost_usd: float
    latency_ms: int
    attempts: int
    status: str
    error_code: str | None


class UsageBucket(Schema):
    calls: int
    cost_usd: float
    input_tokens: int
    output_tokens: int


class LLMUsageOut(Schema):
    days: int
    total_calls: int
    total_cost_usd: float
    today_usd: float
    month_to_date_usd: float
    daily_budget_usd: float
    monthly_budget_usd: float
    by_day: dict[str, float]
    by_model: dict[str, UsageBucket]
    by_purpose: dict[str, UsageBucket]
    by_status: dict[str, int]
    recent: list[LLMCallOut]


@router.get("/admin/llm-usage", response_model=LLMUsageOut)
def llm_usage(
    user: AdminUserDep,
    db: DbSession,
    clock: ClockDep,
    settings: SettingsDep,
    days: Annotated[int, Query(ge=1, le=90)] = 30,
) -> LLMUsageOut:
    r = usage_report(db, clock, settings, days)
    return LLMUsageOut(
        days=r.days,
        total_calls=r.total_calls,
        total_cost_usd=float(r.total_cost_usd),
        today_usd=float(r.today_usd),
        month_to_date_usd=float(r.month_to_date_usd),
        daily_budget_usd=float(r.daily_budget_usd),
        monthly_budget_usd=float(r.monthly_budget_usd),
        by_day={d.isoformat(): float(v) for d, v in r.by_day.items()},
        by_model={k: UsageBucket.model_validate(v) for k, v in r.by_model.items()},
        by_purpose={k: UsageBucket.model_validate(v) for k, v in r.by_purpose.items()},
        by_status=r.by_status,
        recent=[
            LLMCallOut(
                id=c.id,
                at_utc=c.at_utc,
                purpose=c.purpose,
                prompt_id=c.prompt_id,
                prompt_version=c.prompt_version,
                model=c.model,
                input_tokens=c.input_tokens,
                output_tokens=c.output_tokens,
                cost_usd=float(c.cost_usd),
                latency_ms=c.latency_ms,
                attempts=c.attempts,
                status=c.status.value,
                error_code=c.error_code,
            )
            for c in r.recent
        ],
    )
