"""Registration, login, token rotation and logout."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date, timedelta
from functools import lru_cache

from sqlalchemy.orm import Session

from netracare.config import Settings
from netracare.db.models import Patient, RefreshToken, User
from netracare.domain.clock import Clock, clinic_today
from netracare.domain.enums import Role
from netracare.domain.policies import ADULT_AGE, age_on
from netracare.errors import ConflictError, UnauthorizedError, ValidationAppError
from netracare.repositories.directory_repo import UserRepository
from netracare.repositories.token_repo import RefreshTokenRepository
from netracare.security.passwords import hash_password, verify_password
from netracare.security.rate_limit import LoginThrottle
from netracare.security.tokens import create_access_token, hash_refresh_token, new_refresh_token
from netracare.services.audit_service import AuditService

MAX_AGE_YEARS = 120
_GENERIC_LOGIN_FAILURE = "invalid email or password"


@lru_cache(maxsize=1)
def _dummy_hash() -> str:
    # Used to spend the same scrypt time for unknown emails (no user-enumeration timing oracle).
    return hash_password("not-a-real-password")


@dataclass(frozen=True)
class RegisterPatient:
    email: str
    password: str
    full_name: str
    dob: date
    phone: str | None = None


@dataclass(frozen=True)
class TokenPair:
    access_token: str
    refresh_token: str
    expires_in: int
    token_type: str = "bearer"  # noqa: S105


class AuthService:
    def __init__(
        self, session: Session, settings: Settings, clock: Clock, throttle: LoginThrottle
    ) -> None:
        self.s = session
        self.settings = settings
        self.clock = clock
        self.throttle = throttle
        self.users = UserRepository(session)
        self.tokens = RefreshTokenRepository(session)
        self.audit = AuditService(session)

    # -- tokens ------------------------------------------------------------------------------
    def _issue(self, user: User, family_id: str | None = None) -> tuple[TokenPair, RefreshToken]:
        now = self.clock.now_utc()
        raw, digest = new_refresh_token()
        record = RefreshToken(
            user_id=user.id,
            token_hash=digest,
            family_id=family_id or str(uuid.uuid4()),
            expires_at=now + timedelta(days=self.settings.refresh_token_days),
            created_at=now,
        )
        self.tokens.add(record)
        access = create_access_token(user.id, user.role, self.settings, now)
        pair = TokenPair(access, raw, self.settings.access_token_minutes * 60)
        return pair, record

    # -- commands ----------------------------------------------------------------------------
    def register_patient(self, cmd: RegisterPatient) -> TokenPair:
        email = cmd.email.strip().lower()
        today = clinic_today(self.clock, self.settings.tz)
        if cmd.dob > today:
            raise ValidationAppError("invalid_dob", "date of birth cannot be in the future")
        age = age_on(cmd.dob, today)
        if age > MAX_AGE_YEARS:
            raise ValidationAppError("invalid_dob", "date of birth is not plausible")
        if age < ADULT_AGE:
            raise ValidationAppError(
                "minor_self_registration",
                "patients under 18 must be registered by a parent or guardian at the front desk",
            )
        if self.users.by_email(email) is not None:
            raise ConflictError("email_taken", "an account with this email already exists")
        now = self.clock.now_utc()
        user = self.users.add(
            User(email=email, password_hash=hash_password(cmd.password), role=Role.PATIENT)
        )
        patient = Patient(
            user_id=user.id,
            full_name=cmd.full_name.strip(),
            dob=cmd.dob,
            phone=cmd.phone,
            email=email,
            consent_captured_at=now,
        )
        self.s.add(patient)
        self.s.flush()
        self.audit.record(
            user.id, "auth.register", "user", user.id, after={"patient_id": patient.id}
        )
        pair, _ = self._issue(user)
        return pair

    def login(self, email: str, password: str) -> tuple[TokenPair, User]:
        key = email.strip().lower()
        now = self.clock.now_utc()
        self.throttle.check(key, now)
        user = self.users.by_email(key)
        ok = verify_password(password, user.password_hash if user else _dummy_hash())
        if user is None or not ok or not user.is_active:
            self.throttle.record_failure(key, now)
            self.audit.record(
                None,
                "auth.login_failed",
                "user",
                user.id if user else None,
                after={"reason": "inactive" if user and ok else "bad_credentials"},
            )
            raise UnauthorizedError("invalid_credentials", _GENERIC_LOGIN_FAILURE, commit=True)
        self.throttle.reset(key)
        pair, _ = self._issue(user)
        self.audit.record(user.id, "auth.login", "user", user.id, after={"role": user.role.value})
        return pair, user

    def refresh(self, raw_token: str) -> TokenPair:
        now = self.clock.now_utc()
        record = self.tokens.by_hash(hash_refresh_token(raw_token))
        invalid = UnauthorizedError("invalid_refresh_token", "please sign in again")
        if record is None:
            raise invalid
        if record.revoked_at is not None:
            # A rotated-out token was presented again: assume theft, kill the whole family.
            self.tokens.revoke_family(record.family_id, now)
            self.audit.record(
                None,
                "auth.refresh_reuse",
                "user",
                record.user_id,
                after={"family": record.family_id},
            )
            raise UnauthorizedError("refresh_token_reused", "please sign in again", commit=True)
        if record.expires_at <= now:
            raise invalid
        user = self.users.get(record.user_id)
        if user is None or not user.is_active:
            raise invalid
        pair, new_record = self._issue(user, family_id=record.family_id)
        self.tokens.revoke(record, now, replaced_by_id=new_record.id)
        return pair

    def logout(self, raw_token: str) -> None:
        record = self.tokens.by_hash(hash_refresh_token(raw_token))
        if record is not None and record.revoked_at is None:
            self.tokens.revoke(record, self.clock.now_utc())
            self.audit.record(record.user_id, "auth.logout", "user", record.user_id)
