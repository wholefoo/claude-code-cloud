"""Video settings. API keys come from the environment only (bring your own keys)."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class VideoSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="RB_VIDEO_", env_file=".env", extra="ignore")

    niche: str = "technology"
    keywords: list[str] = Field(default_factory=list)  # boosts relevance scoring
    region: str = "US"
    output_dir: Path = Path("./video-output")
    target_seconds: int = 50  # short-form sweet spot
    template: str = "bold"  # bold / clean / news / minimal (see templates.py)
    formats: list[str] = Field(default_factory=lambda: ["9:16"])  # 9:16, 4:5, 1:1, 16:9
    voice_id: str = "21m00Tcm4TlvDq8ikWAM"  # ElevenLabs default voice; override per brand
    captions: Literal["whisper", "even"] = "whisper"  # whisper falls back to even if absent
    whisper_model: str = "base.en"  # faster-whisper model (tiny.en … large-v3)
    subreddits: list[str] = Field(default_factory=list)  # e.g. ["technology", "gadgets"]
    reddit_user_agent: str = "redblue-video/0.1 (trend research; self-hosted)"
    google_trends_geo: str = ""  # defaults to `region`
    # Performance tracking (see performance.py)
    perf_window_hours: int = 72  # compare videos by views at this age
    track_days: int = 30  # keep fetching stats for publications this recent
    learn_from_performance: bool = True  # nudge trend scores by your track record
    learn_min_videos: int = 5  # ...once this many videos have mature stats
    score_weights: dict[str, float] = Field(default_factory=dict)  # override scoring.WEIGHTS
    # Direct upload (see upload.py). Off unless enabled AND an upload token is set; even then
    # a person must press the button (or confirm in the CLI) for every upload.
    upload_enabled: bool = False
    upload_category_id: str = "28"  # YouTube category: 28 = Science & Technology
    # TikTok: "inbox" sends the video to the creator's TikTok drafts to finish in the app
    # (scope video.upload); "direct" posts it (scope video.publish; private until audited).
    tiktok_mode: Literal["inbox", "direct"] = "inbox"
    tiktok_username: str = ""  # optional, for linking finished inbox posts
    tiktok_token_file: Path | None = None  # optional: where rotated refresh tokens are kept
    instagram_graph_host: Literal["graph.facebook.com", "graph.instagram.com"] = (
        "graph.facebook.com"  # Facebook Login; use graph.instagram.com for Instagram Login
    )
    instagram_api_version: str = "v25.0"
    facebook_api_version: str = "v25.0"
    linkedin_version: str = "202606"  # LinkedIn-Version header (YYYYMM)
    # X refresh tokens are single-use: the current one lives in this file (mode 600).
    x_token_file: Path | None = None
    x_max_chars: int = 280  # raise it if the account has a longer post limit
    # Public https address of this RedBlue site, used for Threads' short-lived video links.
    # Defaults to the platform's RB_BASE_URL.
    public_base_url: str = ""
    pinterest_board_id: str = ""  # numeric id of the board Pins go to
    pinterest_api_host: Literal["api.pinterest.com", "api-sandbox.pinterest.com"] = (
        "api.pinterest.com"
    )
    pinterest_token_file: Path | None = None  # keeps Pinterest's renewed refresh tokens
    bluesky_pds: str = "https://bsky.social"  # your PDS if you self-host
    tumblr_blog: str = ""  # blog name (e.g. myblog) posts go to
    tumblr_token_file: Path | None = None  # keeps Tumblr's renewed refresh tokens

    tavily_api_key: SecretStr | None = Field(default=None, alias="TAVILY_API_KEY")
    firecrawl_api_key: SecretStr | None = Field(default=None, alias="FIRECRAWL_API_KEY")
    youtube_api_key: SecretStr | None = Field(default=None, alias="YOUTUBE_API_KEY")
    pexels_api_key: SecretStr | None = Field(default=None, alias="PEXELS_API_KEY")
    elevenlabs_api_key: SecretStr | None = Field(default=None, alias="ELEVENLABS_API_KEY")
    reddit_client_id: str | None = Field(default=None, alias="REDDIT_CLIENT_ID")
    reddit_client_secret: SecretStr | None = Field(default=None, alias="REDDIT_CLIENT_SECRET")
    # Optional, read-only YouTube Analytics (retention). OAuth scope yt-analytics.readonly.
    youtube_oauth_client_id: str | None = Field(default=None, alias="YOUTUBE_OAUTH_CLIENT_ID")
    youtube_oauth_client_secret: SecretStr | None = Field(
        default=None, alias="YOUTUBE_OAUTH_CLIENT_SECRET"
    )
    youtube_oauth_refresh_token: SecretStr | None = Field(
        default=None, alias="YOUTUBE_OAUTH_REFRESH_TOKEN"
    )
    tiktok_client_key: str | None = Field(default=None, alias="TIKTOK_CLIENT_KEY")
    tiktok_client_secret: SecretStr | None = Field(default=None, alias="TIKTOK_CLIENT_SECRET")
    tiktok_refresh_token: SecretStr | None = Field(default=None, alias="TIKTOK_REFRESH_TOKEN")
    instagram_access_token: SecretStr | None = Field(default=None, alias="INSTAGRAM_ACCESS_TOKEN")
    instagram_user_id: str | None = Field(default=None, alias="INSTAGRAM_USER_ID")
    facebook_page_access_token: SecretStr | None = Field(
        default=None, alias="FACEBOOK_PAGE_ACCESS_TOKEN"
    )
    facebook_page_id: str | None = Field(default=None, alias="FACEBOOK_PAGE_ID")
    linkedin_access_token: SecretStr | None = Field(default=None, alias="LINKEDIN_ACCESS_TOKEN")
    linkedin_author_urn: str | None = Field(default=None, alias="LINKEDIN_AUTHOR_URN")
    x_client_id: str | None = Field(default=None, alias="X_CLIENT_ID")
    x_client_secret: SecretStr | None = Field(default=None, alias="X_CLIENT_SECRET")
    x_refresh_token: SecretStr | None = Field(default=None, alias="X_REFRESH_TOKEN")
    threads_access_token: SecretStr | None = Field(default=None, alias="THREADS_ACCESS_TOKEN")
    threads_user_id: str | None = Field(default=None, alias="THREADS_USER_ID")
    reddit_username: str | None = Field(default=None, alias="REDDIT_USERNAME")
    reddit_post_refresh_token: SecretStr | None = Field(
        default=None, alias="REDDIT_POST_REFRESH_TOKEN"
    )
    bluesky_handle: str | None = Field(default=None, alias="BLUESKY_HANDLE")
    bluesky_app_password: SecretStr | None = Field(default=None, alias="BLUESKY_APP_PASSWORD")
    tumblr_client_id: str | None = Field(default=None, alias="TUMBLR_CLIENT_ID")
    tumblr_client_secret: SecretStr | None = Field(default=None, alias="TUMBLR_CLIENT_SECRET")
    tumblr_refresh_token: SecretStr | None = Field(default=None, alias="TUMBLR_REFRESH_TOKEN")
    pinterest_app_id: str | None = Field(default=None, alias="PINTEREST_APP_ID")
    pinterest_app_secret: SecretStr | None = Field(default=None, alias="PINTEREST_APP_SECRET")
    pinterest_refresh_token: SecretStr | None = Field(default=None, alias="PINTEREST_REFRESH_TOKEN")
    # Separate token for uploads (scope youtube.upload), same OAuth client.
    youtube_upload_refresh_token: SecretStr | None = Field(
        default=None, alias="YOUTUBE_UPLOAD_REFRESH_TOKEN"
    )

    @property
    def reddit_credentials(self) -> tuple[str, str] | None:
        if self.reddit_client_id and self.reddit_client_secret:
            return self.reddit_client_id, self.reddit_client_secret.get_secret_value()
        return None

    @property
    def youtube_oauth(self) -> tuple[str, str, str] | None:
        if (
            self.youtube_oauth_client_id
            and self.youtube_oauth_client_secret
            and self.youtube_oauth_refresh_token
        ):
            return (
                self.youtube_oauth_client_id,
                self.youtube_oauth_client_secret.get_secret_value(),
                self.youtube_oauth_refresh_token.get_secret_value(),
            )
        return None

    @property
    def youtube_upload_oauth(self) -> tuple[str, str, str] | None:
        if (
            self.youtube_oauth_client_id
            and self.youtube_oauth_client_secret
            and self.youtube_upload_refresh_token
        ):
            return (
                self.youtube_oauth_client_id,
                self.youtube_oauth_client_secret.get_secret_value(),
                self.youtube_upload_refresh_token.get_secret_value(),
            )
        return None

    @property
    def tiktok_credentials(self) -> tuple[str, str, str] | None:
        """(client key, secret, refresh token). A token file, if set and present, wins over
        the environment, because TikTok may rotate refresh tokens."""
        refresh = self.tiktok_refresh_token.get_secret_value() if self.tiktok_refresh_token else ""
        if self.tiktok_token_file and self.tiktok_token_file.is_file():
            refresh = self.tiktok_token_file.read_text(encoding="utf-8").strip() or refresh
        if self.tiktok_client_key and self.tiktok_client_secret and refresh:
            return self.tiktok_client_key, self.tiktok_client_secret.get_secret_value(), refresh
        return None

    @property
    def instagram_credentials(self) -> tuple[str, str] | None:
        if self.instagram_access_token and self.instagram_user_id:
            return self.instagram_access_token.get_secret_value(), self.instagram_user_id
        return None

    @property
    def facebook_credentials(self) -> tuple[str, str] | None:
        if self.facebook_page_access_token and self.facebook_page_id:
            return self.facebook_page_access_token.get_secret_value(), self.facebook_page_id
        return None

    @property
    def linkedin_credentials(self) -> tuple[str, str] | None:
        if self.linkedin_access_token and self.linkedin_author_urn:
            return self.linkedin_access_token.get_secret_value(), self.linkedin_author_urn
        return None

    @property
    def x_credentials(self) -> tuple[str, str, str] | None:
        """(client id, client secret or "", refresh token). Needs the token file: X refresh
        tokens are single-use, so the environment value only seeds the file once."""
        if not (self.x_client_id and self.x_token_file):
            return None
        refresh = ""
        if self.x_token_file.is_file():
            refresh = self.x_token_file.read_text(encoding="utf-8").strip()
        if not refresh and self.x_refresh_token:
            refresh = self.x_refresh_token.get_secret_value()
        if not refresh:
            return None
        secret = self.x_client_secret.get_secret_value() if self.x_client_secret else ""
        return self.x_client_id, secret, refresh

    @property
    def threads_credentials(self) -> tuple[str, str] | None:
        if self.threads_access_token and self.threads_user_id:
            return self.threads_access_token.get_secret_value(), self.threads_user_id
        return None

    @property
    def pinterest_credentials(self) -> tuple[str, str, str] | None:
        """(app id, secret, refresh token); a token file, if present, wins over the env."""
        refresh = (
            self.pinterest_refresh_token.get_secret_value() if self.pinterest_refresh_token else ""
        )
        if self.pinterest_token_file and self.pinterest_token_file.is_file():
            refresh = self.pinterest_token_file.read_text(encoding="utf-8").strip() or refresh
        if self.pinterest_app_id and self.pinterest_app_secret and refresh:
            return self.pinterest_app_id, self.pinterest_app_secret.get_secret_value(), refresh
        return None

    @property
    def bluesky_credentials(self) -> tuple[str, str] | None:
        if self.bluesky_handle and self.bluesky_app_password:
            return self.bluesky_handle.lstrip("@"), self.bluesky_app_password.get_secret_value()
        return None

    @property
    def tumblr_credentials(self) -> tuple[str, str, str] | None:
        """(client id, secret, refresh token); a token file, if present, wins over the env."""
        refresh = self.tumblr_refresh_token.get_secret_value() if self.tumblr_refresh_token else ""
        if self.tumblr_token_file and self.tumblr_token_file.is_file():
            refresh = self.tumblr_token_file.read_text(encoding="utf-8").strip() or refresh
        if self.tumblr_client_id and self.tumblr_client_secret and refresh:
            return self.tumblr_client_id, self.tumblr_client_secret.get_secret_value(), refresh
        return None

    def key(self, name: str) -> str | None:
        value = getattr(self, f"{name}_api_key")
        return value.get_secret_value() if value else None


@lru_cache
def get_video_settings() -> VideoSettings:
    return VideoSettings()
