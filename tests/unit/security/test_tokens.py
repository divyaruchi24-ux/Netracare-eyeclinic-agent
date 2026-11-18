from __future__ import annotations

from datetime import UTC, datetime, timedelta

import jwt
import pytest

from netracare.config import Settings
from netracare.domain.enums import Role
from netracare.errors import UnauthorizedError
from netracare.security.tokens import (
    create_access_token,
    decode_access_token,
    hash_refresh_token,
    new_refresh_token,
)

NOW = datetime(2026, 11, 10, 3, 30, tzinfo=UTC)
S = Settings(_env_file=None, jwt_secret="k" * 40)  # type: ignore[call-arg,arg-type]


def test_roundtrip() -> None:
    claims = decode_access_token(create_access_token("u1", Role.ADMIN, S, NOW), S, NOW)
    assert (claims.user_id, claims.role) == ("u1", Role.ADMIN)
    assert claims.expires_at == NOW + timedelta(minutes=15)


def test_expiry_uses_injected_clock() -> None:
    tok = create_access_token("u1", Role.PATIENT, S, NOW)
    decode_access_token(tok, S, NOW + timedelta(minutes=14, seconds=59))
    with pytest.raises(UnauthorizedError) as e:
        decode_access_token(tok, S, NOW + timedelta(minutes=15))
    assert e.value.code == "token_expired"


def test_wrong_secret_rejected() -> None:
    other = Settings(_env_file=None, jwt_secret="z" * 40)  # type: ignore[call-arg,arg-type]
    with pytest.raises(UnauthorizedError):
        decode_access_token(create_access_token("u1", Role.PATIENT, other, NOW), S, NOW)


@pytest.mark.parametrize(
    "payload",
    [
        {"sub": "u1", "role": "admin", "type": "refresh", "exp": 9_999_999_999},
        {"sub": "u1", "role": "superuser", "type": "access", "exp": 9_999_999_999},
        {"sub": "u1", "role": "admin", "exp": 9_999_999_999},
    ],
)
def test_bad_claims_rejected(payload: dict[str, object]) -> None:
    tok = jwt.encode(payload, S.jwt_secret.get_secret_value(), algorithm="HS256")
    with pytest.raises(UnauthorizedError):
        decode_access_token(tok, S, NOW)


def test_alg_none_rejected() -> None:
    tok = jwt.encode(
        {"sub": "u1", "role": "admin", "type": "access", "exp": 9_999_999_999},
        None,
        algorithm="none",
    )
    with pytest.raises(UnauthorizedError):
        decode_access_token(tok, S, NOW)


def test_refresh_token_hashing() -> None:
    raw, digest = new_refresh_token()
    assert len(raw) > 40 and digest == hash_refresh_token(raw) and raw not in digest
