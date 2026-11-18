from __future__ import annotations

import pytest

from netracare.config import Settings, assert_safe_secrets


def _s(**kw: object) -> Settings:
    return Settings(_env_file=None, **kw)  # type: ignore[arg-type,call-arg]


def test_assert_safe_secrets_dev_ok() -> None:
    assert_safe_secrets(_s(environment="dev"))


def test_assert_safe_secrets_prod_default_fails() -> None:
    with pytest.raises(RuntimeError, match="JWT_SECRET"):
        assert_safe_secrets(_s(environment="prod"))


def test_assert_safe_secrets_prod_short_secret_fails() -> None:
    with pytest.raises(RuntimeError, match="JWT_SECRET"):
        assert_safe_secrets(
            _s(environment="prod", jwt_secret="short", seed_default_password="x" * 12)
        )


def test_assert_safe_secrets_prod_default_seed_password_fails() -> None:
    with pytest.raises(RuntimeError, match="SEED_DEFAULT_PASSWORD"):
        assert_safe_secrets(_s(environment="prod", jwt_secret="y" * 40))


def test_assert_safe_secrets_prod_strong_ok() -> None:
    assert_safe_secrets(_s(environment="prod", jwt_secret="y" * 40, seed_default_password="z" * 16))
