"""Admin user management (staff accounts, roles, activation)."""

from __future__ import annotations

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from netracare.db.models import Doctor, StaffUser, User
from netracare.domain.actor import CurrentUser
from netracare.domain.clock import Clock
from netracare.domain.enums import Role
from netracare.errors import ConflictError, NotFoundError, ValidationAppError
from netracare.repositories.directory_repo import UserRepository
from netracare.repositories.token_repo import RefreshTokenRepository
from netracare.security.passwords import hash_password
from netracare.services.audit_service import AuditService

STAFF_ROLES = frozenset({Role.FRONT_DESK, Role.ADMIN, Role.DOCTOR})


class UserService:
    def __init__(self, session: Session, clock: Clock) -> None:
        self.s = session
        self.clock = clock
        self.users = UserRepository(session)
        self.audit = AuditService(session)

    def list_users(
        self, role: Role | None, q: str | None, limit: int, offset: int
    ) -> tuple[list[tuple[User, StaffUser | None]], int]:
        stmt = select(User, StaffUser).outerjoin(StaffUser, StaffUser.user_id == User.id)
        if role is not None:
            stmt = stmt.where(User.role == role)
        if q:
            like = f"%{q.strip().lower()}%"
            stmt = stmt.where(
                or_(
                    func.lower(User.email).like(like), func.lower(StaffUser.display_name).like(like)
                )
            )
        total = int(self.s.scalar(select(func.count()).select_from(stmt.subquery())) or 0)
        rows = self.s.execute(stmt.order_by(User.role, User.email).limit(limit).offset(offset))
        return [(u, st) for u, st in rows], total

    def create_staff(
        self,
        actor: CurrentUser,
        *,
        email: str,
        display_name: str,
        role: Role,
        temp_password: str,
        doctor_id: str | None = None,
    ) -> tuple[User, StaffUser]:
        if role not in STAFF_ROLES:
            raise ValidationAppError(
                "invalid_role", "staff role must be front_desk, doctor or admin"
            )
        if (role is Role.DOCTOR) != (doctor_id is not None):
            raise ValidationAppError(
                "doctor_link", "doctor accounts need doctor_id; other roles must not have one"
            )
        if doctor_id is not None:
            if self.s.get(Doctor, doctor_id) is None:
                raise NotFoundError("doctor_not_found", "doctor not found")
            if self.s.scalar(select(StaffUser).where(StaffUser.doctor_id == doctor_id)):
                raise ConflictError("doctor_has_account", "that doctor already has an account")
        email = email.strip().lower()
        if self.users.by_email(email) is not None:
            raise ConflictError("email_taken", "an account with this email already exists")
        user = self.users.add(
            User(email=email, password_hash=hash_password(temp_password), role=role)
        )
        staff = StaffUser(user_id=user.id, display_name=display_name, doctor_id=doctor_id)
        self.s.add(staff)
        self.s.flush()
        self.audit.record(
            actor,
            "user.create",
            "user",
            user.id,
            after={"role": role.value, "doctor_id": doctor_id},
        )
        return user, staff

    def _active_admins(self) -> int:
        return int(
            self.s.scalar(
                select(func.count())
                .select_from(User)
                .where(User.role == Role.ADMIN, User.is_active)
            )
            or 0
        )

    def update_user(
        self, actor: CurrentUser, user_id: str, *, role: Role | None, is_active: bool | None
    ) -> User:
        user = self.users.get(user_id)
        if user is None:
            raise NotFoundError("user_not_found", "user not found")
        before = {"role": user.role.value, "is_active": user.is_active}
        if role is not None and role != user.role:
            if Role.PATIENT in (role, user.role):
                raise ValidationAppError(
                    "invalid_role_change", "patient accounts cannot change role"
                )
            if role is Role.DOCTOR or user.role is Role.DOCTOR:
                raise ValidationAppError(
                    "invalid_role_change", "doctor accounts are linked to a doctor record"
                )
        losing_admin = (
            user.role is Role.ADMIN
            and user.is_active
            and ((role is not None and role is not Role.ADMIN) or is_active is False)
        )
        if user.id == actor.id and (is_active is False or losing_admin):
            raise ConflictError("cannot_demote_self", "you cannot deactivate or demote yourself")
        if losing_admin and self._active_admins() <= 1:
            raise ConflictError("last_admin", "at least one active admin is required")
        if role is not None:
            user.role = role
        if is_active is not None:
            user.is_active = is_active
            if not is_active:
                RefreshTokenRepository(self.s).revoke_all_for_user(user.id, self.clock.now_utc())
        self.s.flush()
        self.audit.record(
            actor,
            "user.update",
            "user",
            user.id,
            before=before,
            after={"role": user.role.value, "is_active": user.is_active},
        )
        return user
