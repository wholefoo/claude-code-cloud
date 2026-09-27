"""Platform configuration, read from environment variables (prefix ``RB_``).

Secrets live in the environment or a secret manager, never in the repo or the CMS.
"""

from __future__ import annotations

import secrets
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class AgentModels(BaseSettings):
    """Default model per agent role (plan §5). Override with RB_MODEL_<ROLE>."""

    model_config = SettingsConfigDict(env_prefix="RB_MODEL_", extra="ignore")

    builder: str = "claude-sonnet-5"
    red: str = "claude-haiku-4-5-20251001"
    blue: str = "claude-sonnet-5"
    content: str = "claude-sonnet-5"
    growth_audit: str = "claude-haiku-4-5-20251001"
    growth: str = "claude-sonnet-5"
    ops_triage: str = "claude-haiku-4-5-20251001"
    ops: str = "claude-sonnet-5"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="RB_", env_file=".env", extra="ignore")

    env: Literal["dev", "test", "prod"] = "dev"
    site_name: str = "My RedBlue Site"
    base_url: str = "http://localhost:8000"
    database_url: str = "sqlite:///./redblue.db"
    secret_key: SecretStr = Field(default_factory=lambda: SecretStr(secrets.token_urlsafe(48)))
    storage_dir: Path = Path("./media")
    storage_backend: Literal["local", "s3"] = "local"
    s3_bucket: str | None = None
    s3_endpoint_url: str | None = None
    max_upload_bytes: int = 10 * 1024 * 1024
    session_max_age: int = 60 * 60 * 12
    email_provider: Literal["console", "smtp"] = "console"
    email_from: str = "no-reply@example.com"
    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_user: str | None = None
    smtp_password: SecretStr | None = None
    # BYOK: users supply their own key; read from the standard variable.
    anthropic_api_key: SecretStr | None = Field(default=None, alias="ANTHROPIC_API_KEY")
    agent_spend_limit_usd: float = 5.0  # per agent, per day
    ai_crawlers_allowed: bool = True
    ai_crawler_overrides: dict[str, bool] = Field(default_factory=dict)
    analytics_enabled: bool = True
    default_locale: str = "en"
    locales: list[str] = Field(default_factory=lambda: ["en"])
    organization_name: str | None = None
    organization_logo: str | None = None
    maintenance_mode: bool = False
    theme_tokens: dict = Field(default_factory=dict)  # overrides for redblue.templates.Tokens
    min_publish_words: int = 150  # growth guardrail: thin-content threshold
    max_duplicate_similarity: float = 0.8

    @field_validator("base_url")
    @classmethod
    def _strip_slash(cls, v: str) -> str:
        return v.rstrip("/")

    @property
    def is_prod(self) -> bool:
        return self.env == "prod"

    @property
    def secure_cookies(self) -> bool:
        return self.base_url.startswith("https://")

    @property
    def models(self) -> AgentModels:
        return AgentModels()

    def validate_for_production(self) -> list[str]:
        """Problems that must be fixed before running with env=prod (used by `redblue doctor`)."""
        problems: list[str] = []
        if "RB_SECRET_KEY" not in _env_keys():
            problems.append("RB_SECRET_KEY is not set; sessions will not survive restarts.")
        elif len(self.secret_key.get_secret_value()) < 32:
            problems.append("RB_SECRET_KEY must be at least 32 characters.")
        if not self.base_url.startswith("https://"):
            problems.append("RB_BASE_URL should use https:// in production.")
        if self.database_url.startswith("sqlite"):
            problems.append("SQLite is for local development; use Postgres in production.")
        return problems


def _env_keys() -> set[str]:
    import os

    return set(os.environ)


@lru_cache
def get_settings() -> Settings:
    return Settings()
