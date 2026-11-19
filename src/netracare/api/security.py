"""Authentication (bearer JWT) and role-based route guards."""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated

import structlog
from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select

from netracare.api.deps import ClockDep, DbSession, SettingsDep
from netracare.db.models import Patient, StaffUser, User
from netracare.domain.actor import CurrentUser
from netracare.domain.enums import Role
from netracare.errors import ForbiddenError, UnauthorizedError
from netracare.security.tokens import decode_access_token

bearer = HTTPBearer(auto_error=False)
BearerCreds = Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)]


def current_user(
    creds: BearerCreds, db: DbSession, settings: SettingsDep, clock: ClockDep
) -> CurrentUser:
    if creds is None or creds.scheme.lower() != "bearer":
        raise UnauthorizedError("not_authenticated", "sign in required")
    claims = decode_access_token(creds.credentials, settings, clock.now_utc())
    user = db.get(User, claims.user_id)
    # The DB is the source of truth for activity and role (a token outlives a role change).
    if user is None or not user.is_active:
        raise UnauthorizedError("invalid_token", "invalid or expired token")
    staff = db.scalar(select(StaffUser).where(StaffUser.user_id == user.id))
    if staff is not None:
        display = staff.display_name
    else:
        own = db.scalar(select(Patient.full_name).where(Patient.user_id == user.id))
        display = own or user.email
    structlog.contextvars.bind_contextvars(user_role=user.role.value)
    return CurrentUser(
        id=user.id,
        role=user.role,
        email=user.email,
        display_name=display,
        doctor_id=staff.doctor_id if staff else None,
    )


CurrentUserDep = Annotated[CurrentUser, Depends(current_user)]


def require_roles(*roles: Role) -> Callable[[CurrentUser], CurrentUser]:
    allowed = frozenset(roles)

    def _guard(user: CurrentUserDep) -> CurrentUser:
        if user.role not in allowed:
            raise ForbiddenError("forbidden", "your role cannot perform this action")
        return user

    return _guard


StaffUserDep = Annotated[CurrentUser, Depends(require_roles(Role.FRONT_DESK, Role.ADMIN))]
AdminUserDep = Annotated[CurrentUser, Depends(require_roles(Role.ADMIN))]
DoctorUserDep = Annotated[CurrentUser, Depends(require_roles(Role.DOCTOR))]
BookerUserDep = Annotated[
    CurrentUser, Depends(require_roles(Role.PATIENT, Role.FRONT_DESK, Role.ADMIN))
]
ClinicianUserDep = Annotated[
    CurrentUser, Depends(require_roles(Role.DOCTOR, Role.FRONT_DESK, Role.ADMIN))
]
PatientUserDep = Annotated[CurrentUser, Depends(require_roles(Role.PATIENT))]


def optional_user(
    creds: BearerCreds, db: DbSession, settings: SettingsDep, clock: ClockDep
) -> CurrentUser | None:
    """The caller if a bearer token is sent (it must be valid), else None (anonymous)."""
    if creds is None:
        return None
    return current_user(creds, db, settings, clock)


OptionalUserDep = Annotated[CurrentUser | None, Depends(optional_user)]
