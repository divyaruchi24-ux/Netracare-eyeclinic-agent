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


def test_env_example_lists_every_setting_and_holds_no_secrets() -> None:
    """The public template must stay complete and secret-free (it has been edited by hand
    before: a Phase 3 commit lost a dozen lines and gained a workspace name)."""
    from pathlib import Path

    from netracare.config import Settings

    root = Path(__file__).resolve().parents[2]
    lines = (root / ".env.example").read_text(encoding="utf-8").splitlines()
    values = dict(line.split("=", 1) for line in lines if "=" in line and not line.startswith("#"))
    missing = [f.upper() for f in Settings.model_fields if f.upper() not in values]
    assert missing == [], f"add to .env.example: {missing}"
    for secret in ("GROQ_API_KEY", "OPIK_API_KEY", "OPIK_WORKSPACE"):
        assert values[secret] == "", f"{secret} must be empty in the public template"
