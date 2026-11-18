"""Access JWTs (short-lived) and opaque refresh tokens (stored hashed)."""

from __future__ import annotations

import hashlib
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import jwt

from netracare.config import Settings
from netracare.domain.enums import Role
from netracare.errors import UnauthorizedError

ALGORITHM = "HS256"
ACCESS_TYPE = "access"


@dataclass(frozen=True)
class AccessClaims:
    user_id: str
    role: Role
    expires_at: datetime
    jti: str


def create_access_token(user_id: str, role: Role, settings: Settings, now: datetime) -> str:
    exp = now + timedelta(minutes=settings.access_token_minutes)
    payload = {
        "sub": user_id,
        "role": role.value,
        "type": ACCESS_TYPE,
        "iat": int(now.timestamp()),
        "exp": int(exp.timestamp()),
        "jti": uuid.uuid4().hex,
    }
    return jwt.encode(payload, settings.jwt_secret.get_secret_value(), algorithm=ALGORITHM)


def decode_access_token(token: str, settings: Settings, now: datetime) -> AccessClaims:
    """Validate signature, type and expiry against the injected clock (not wall time)."""
    invalid = UnauthorizedError("invalid_token", "invalid or expired token")
    try:
        payload = jwt.decode(
            token,
            settings.jwt_secret.get_secret_value(),
            algorithms=[ALGORITHM],
            options={"verify_exp": False, "verify_iat": False, "require": ["sub", "exp", "type"]},
        )
    except jwt.PyJWTError:
        raise invalid from None
    if payload.get("type") != ACCESS_TYPE:
        raise invalid
    expires_at = datetime.fromtimestamp(int(payload["exp"]), UTC)
    if now >= expires_at:
        raise UnauthorizedError("token_expired", "access token expired")
    try:
        role = Role(payload["role"])
    except (KeyError, ValueError):
        raise invalid from None
    return AccessClaims(
        user_id=str(payload["sub"]), role=role, expires_at=expires_at, jti=str(payload.get("jti"))
    )


def hash_refresh_token(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def new_refresh_token() -> tuple[str, str]:
    """Return (raw token for the client, sha256 hash for storage)."""
    raw = secrets.token_urlsafe(48)
    return raw, hash_refresh_token(raw)
