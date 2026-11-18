"""Application settings loaded from environment variables / `.env`."""

from __future__ import annotations

from functools import lru_cache
from zoneinfo import ZoneInfo

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Core
    environment: str = "dev"
    log_level: str = "INFO"
    database_url: str = "sqlite:///./data/netracare.db"
    jwt_secret: SecretStr = SecretStr("change-me-dev-only")
    clinic_timezone: str = "Asia/Kolkata"
    seed_default_password: SecretStr = SecretStr("NetraCare#Dev1")

    # LLM (Groq)
    groq_api_key: SecretStr | None = None
    groq_model_reasoning: str = "openai/gpt-oss-120b"
    groq_model_fast: str = "openai/gpt-oss-20b"

    # LLM-as-judge
    judge_provider: str = "groq"
    judge_model: str = "qwen/qwen3.8-27b"
    judge_reasoning_effort: str = "none"
    judge_max_tokens: int = 900

    # Observability
    opik_api_key: SecretStr | None = None
    opik_workspace: str | None = None
    opik_project_name: str = "netracare"

    # Retrieval
    embedding_model: str = "BAAI/bge-small-en-v1.5"
    reranker_model: str = "cross-encoder/ms-marco-MiniLM-L-6-v2"
    chroma_path: str = "./data/chroma"

    # Budgets (USD)
    monthly_llm_budget_usd: float = Field(default=20.0, ge=0)
    conversation_budget_usd: float = Field(default=0.05, ge=0)

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.clinic_timezone)


@lru_cache
def get_settings() -> Settings:
    return Settings()
