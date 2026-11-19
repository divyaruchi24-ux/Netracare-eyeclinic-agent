"""Application settings loaded from environment variables / `.env`."""

from __future__ import annotations

from functools import lru_cache
from zoneinfo import ZoneInfo

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_JWT_SECRET = "change-me-dev-only"  # noqa: S105 - dev default, refused outside dev
DEFAULT_SEED_PASSWORD = "NetraCare#Dev1"  # noqa: S105 - dev default, refused outside dev
MIN_JWT_SECRET_LEN = 32


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Core
    environment: str = "dev"
    log_level: str = "INFO"
    database_url: str = "sqlite:///./data/netracare.db"
    jwt_secret: SecretStr = SecretStr(DEFAULT_JWT_SECRET)
    clinic_timezone: str = "Asia/Kolkata"
    seed_default_password: SecretStr = SecretStr(DEFAULT_SEED_PASSWORD)

    # Auth / API
    access_token_minutes: int = Field(default=15, ge=1, le=120)
    refresh_token_days: int = Field(default=7, ge=1, le=90)
    api_base_url: str = "http://localhost:8000"
    cors_origins: list[str] = ["http://localhost:8501"]

    # LLM (Groq)
    groq_api_key: SecretStr | None = None
    groq_model_reasoning: str = "openai/gpt-oss-120b"
    groq_model_fast: str = "openai/gpt-oss-20b"
    llm_enabled: bool = True
    llm_timeout_fast_s: float = Field(default=15.0, gt=0)
    llm_timeout_reasoning_s: float = Field(default=45.0, gt=0)
    llm_max_retries: int = Field(default=3, ge=1, le=6)
    prompts_dir: str = "prompts"
    pricing_file: str = "config/model_pricing.yaml"

    # Triage
    triage_confidence_threshold: float = Field(default=0.6, ge=0, le=1)
    triage_rate_limit_per_min: int = Field(default=10, ge=1)

    # LLM-as-judge
    judge_provider: str = "groq"
    judge_model: str = "qwen/qwen3.8-27b"
    judge_reasoning_effort: str = "none"
    judge_max_tokens: int = 900

    # Observability
    opik_api_key: SecretStr | None = None
    opik_workspace: str | None = None
    opik_project_name: str = "netracare"
    opik_enabled: bool = False
    opik_url_override: str | None = None

    # Retrieval
    embedding_model: str = "BAAI/bge-small-en-v1.5"
    reranker_model: str = "cross-encoder/ms-marco-MiniLM-L-6-v2"
    chroma_path: str = "./data/chroma"

    # Budgets (USD)
    monthly_llm_budget_usd: float = Field(default=20.0, ge=0)
    daily_llm_budget_usd: float = Field(default=1.5, ge=0)
    conversation_budget_usd: float = Field(default=0.05, ge=0)

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.clinic_timezone)


def assert_safe_secrets(s: Settings) -> None:
    """Refuse to start outside dev with default or weak secrets."""
    if s.environment == "dev":
        return
    problems: list[str] = []
    secret = s.jwt_secret.get_secret_value()
    if secret == DEFAULT_JWT_SECRET or len(secret) < MIN_JWT_SECRET_LEN:
        problems.append(f"JWT_SECRET must be a non-default value of >= {MIN_JWT_SECRET_LEN} chars")
    if s.seed_default_password.get_secret_value() == DEFAULT_SEED_PASSWORD:
        problems.append("SEED_DEFAULT_PASSWORD must not be the default")
    if problems:
        raise RuntimeError(
            f"unsafe configuration for environment={s.environment!r}: " + "; ".join(problems)
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()
