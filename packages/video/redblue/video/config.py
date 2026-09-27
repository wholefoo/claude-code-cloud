"""Video settings. API keys come from the environment only (bring your own keys)."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class VideoSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="RB_VIDEO_", env_file=".env", extra="ignore")

    niche: str = "technology"
    keywords: list[str] = Field(default_factory=list)  # boosts relevance scoring
    region: str = "US"
    output_dir: Path = Path("./video-output")
    target_seconds: int = 50  # short-form sweet spot
    width: int = 1080
    height: int = 1920
    voice_id: str = "21m00Tcm4TlvDq8ikWAM"  # ElevenLabs default voice; override per brand
    subreddits: list[str] = Field(default_factory=list)  # e.g. ["technology", "gadgets"]
    reddit_user_agent: str = "redblue-video/0.1 (trend research; self-hosted)"
    google_trends_geo: str = ""  # defaults to `region`

    tavily_api_key: SecretStr | None = Field(default=None, alias="TAVILY_API_KEY")
    firecrawl_api_key: SecretStr | None = Field(default=None, alias="FIRECRAWL_API_KEY")
    youtube_api_key: SecretStr | None = Field(default=None, alias="YOUTUBE_API_KEY")
    pexels_api_key: SecretStr | None = Field(default=None, alias="PEXELS_API_KEY")
    elevenlabs_api_key: SecretStr | None = Field(default=None, alias="ELEVENLABS_API_KEY")
    reddit_client_id: str | None = Field(default=None, alias="REDDIT_CLIENT_ID")
    reddit_client_secret: SecretStr | None = Field(default=None, alias="REDDIT_CLIENT_SECRET")

    @property
    def reddit_credentials(self) -> tuple[str, str] | None:
        if self.reddit_client_id and self.reddit_client_secret:
            return self.reddit_client_id, self.reddit_client_secret.get_secret_value()
        return None

    def key(self, name: str) -> str | None:
        value = getattr(self, f"{name}_api_key")
        return value.get_secret_value() if value else None


@lru_cache
def get_video_settings() -> VideoSettings:
    return VideoSettings()
