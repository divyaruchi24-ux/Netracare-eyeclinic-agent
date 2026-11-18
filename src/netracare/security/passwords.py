"""Password hashing with stdlib scrypt (no extra deps). Format: scrypt$n$r$p$salt_hex$hash_hex."""

from __future__ import annotations

import hashlib
import hmac
import secrets

_N, _R, _P, _DKLEN = 2**14, 8, 1, 64


def hash_password(password: str) -> str:
    if not password:
        raise ValueError("password must not be empty")
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=_N, r=_R, p=_P, dklen=_DKLEN)
    return f"scrypt${_N}${_R}${_P}${salt.hex()}${digest.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        algo, n, r, p, salt_hex, hash_hex = encoded.split("$")
    except ValueError:
        return False
    if algo != "scrypt":
        return False
    digest = hashlib.scrypt(
        password.encode(),
        salt=bytes.fromhex(salt_hex),
        n=int(n),
        r=int(r),
        p=int(p),
        dklen=len(hash_hex) // 2,
    )
    return hmac.compare_digest(digest.hex(), hash_hex)
