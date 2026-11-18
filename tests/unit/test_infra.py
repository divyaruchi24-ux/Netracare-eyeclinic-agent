import json
import logging

import pytest

from netracare.config import Settings
from netracare.log import configure_logging, get_logger, redact
from netracare.security.passwords import hash_password, verify_password


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("mail me at asha.k@example.com please", "mail me at [EMAIL] please"),
        ("call +91 90000 00012 now", "call [PHONE] now"),
        ("my pressure is 24", "my pressure is 24"),
        ("slot at 09:15 on 2026-11-10", "slot at 09:15 on 2026-11-10"),
        ("ring 9876543210", "ring [PHONE]"),
        ("landline 080-41234567", "landline [PHONE]"),
        ("between 2026-11-10 and 2026-11-12", "between 2026-11-10 and 2026-11-12"),
        ("order 12345", "order 12345"),
    ],
)
def test_redact(raw: str, expected: str) -> None:
    assert redact(raw) == expected


def test_logger_emits_redacted_json(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("INFO")
    get_logger("t").info("contact", email="x.y@example.com")
    out = capsys.readouterr()
    line = (out.out or out.err).strip().splitlines()[-1]
    payload = json.loads(line)
    assert payload["email"] == "[EMAIL]"
    logging.getLogger().handlers.clear()


def test_password_round_trip() -> None:
    h = hash_password("s3cret!")
    assert h.startswith("scrypt$")
    assert verify_password("s3cret!", h)
    assert not verify_password("nope", h)
    assert hash_password("s3cret!") != h  # salted


@pytest.mark.parametrize("bad", ["", "plain", "bcrypt$1$2$3$aa$bb"])
def test_verify_rejects_malformed(bad: str) -> None:
    assert not verify_password("x", bad)


def test_empty_password_rejected() -> None:
    with pytest.raises(ValueError):
        hash_password("")


def test_settings_defaults_match_requested_models(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in ("GROQ_MODEL_REASONING", "GROQ_MODEL_FAST", "JUDGE_MODEL", "JUDGE_MAX_TOKENS"):
        monkeypatch.delenv(var, raising=False)
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    assert s.groq_model_reasoning == "openai/gpt-oss-120b"
    assert s.groq_model_fast == "openai/gpt-oss-20b"
    assert s.judge_provider == "groq"
    assert s.judge_model == "qwen/qwen3.8-27b"
    assert s.judge_reasoning_effort == "none"
    assert s.judge_max_tokens == 900
    assert str(s.tz) == "Asia/Kolkata"
