"""Person-confirmed uploads to TikTok and Instagram (YouTube is in :mod:`upload`).

Same rule as YouTube: nothing goes out unless a person presses the button (or confirms in
the CLI), only approved projects, and the global ``RB_VIDEO_UPLOAD_ENABLED`` switch.

- **TikTok, inbox mode (default):** the video lands in the creator's TikTok drafts; they
  finish (sound, cover, caption, privacy) and post it in the TikTok app.
- **TikTok, direct mode:** posts it. The person picks the privacy from the options TikTok
  returns for the account (no default), and interaction settings start off. Unaudited TikTok
  apps can only post privately ("only me").
- **Instagram:** step 1 uploads the Reel into a container, which isn't public. Step 2 is a
  separate click on **Publish** (Instagram has no private posts), after the container
  finishes processing. Containers expire after 24 hours.

:func:`refresh` only *reads* status. It's safe to run from the tracking job, and it never
publishes: Instagram publishing happens only in :func:`publish_instagram`.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

import httpx
from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from redblue.core.db import utcnow
from redblue.video import clients, performance
from redblue.video.config import VideoSettings
from redblue.video.models import Upload, VideoProject
from redblue.video.render import media_duration
from redblue.video.schemas import Script
from redblue.video.templates import get_format
from redblue.video.upload import UploadDisabled, render_file

log = logging.getLogger("redblue.video.upload")

PENDING = ("processing", "in_inbox", "ready")
TIKTOK_PRIVACY = (
    "PUBLIC_TO_EVERYONE",
    "MUTUAL_FOLLOW_FRIENDS",
    "FOLLOWER_OF_CREATOR",
    "SELF_ONLY",
)
PRIVACY_LABELS = {
    "PUBLIC_TO_EVERYONE": "Everyone",
    "MUTUAL_FOLLOW_FRIENDS": "Friends",
    "FOLLOWER_OF_CREATOR": "Followers",
    "SELF_ONLY": "Only me",
}


def available(s: VideoSettings) -> dict[str, bool]:
    return {
        "tiktok": s.upload_enabled and s.tiktok_credentials is not None,
        "instagram": s.upload_enabled and s.instagram_credentials is not None,
    }


def _caption_default(p: VideoProject, limit: int) -> str:
    script = Script.model_validate(p.script) if p.script else None
    parts = [script.title if script else p.topic]
    if p.ai_generated:
        parts.append("Made with AI assistance and reviewed by a human.")
    if script and script.hashtags:
        parts.append(" ".join("#" + h.lstrip("#") for h in script.hashtags[:10]))
    return "\n\n".join(parts)[:limit]


def defaults(p: VideoProject) -> dict:
    return {
        "tiktok_caption": _caption_default(p, 2200),
        "instagram_caption": _caption_default(p, 2200),
    }


class TikTokRequest(BaseModel):
    format: str = "9:16"
    caption: str = Field(default="", max_length=2200)
    privacy: str | None = None  # direct mode only; must be one TikTok offered the account
    allow_comments: bool = False
    allow_duet: bool = False
    allow_stitch: bool = False
    is_aigc: bool = True  # TikTok shows an "AI-generated" label
    brand_organic: bool = False  # promoting your own business
    brand_content: bool = False  # paid partnership

    @field_validator("format")
    @classmethod
    def _format(cls, v: str) -> str:
        return get_format(v).key

    @model_validator(mode="after")
    def _rules(self) -> TikTokRequest:
        if self.privacy is not None and self.privacy not in TIKTOK_PRIVACY:
            raise ValueError("Unknown TikTok privacy option.")
        if self.brand_content and self.privacy == "SELF_ONLY":
            raise ValueError("Branded content can't be private on TikTok.")
        return self

    def post_info(self) -> dict:
        return {
            "title": self.caption,
            "privacy_level": self.privacy,
            "disable_comment": not self.allow_comments,
            "disable_duet": not self.allow_duet,
            "disable_stitch": not self.allow_stitch,
            "brand_organic_toggle": self.brand_organic,
            "brand_content_toggle": self.brand_content,
            "is_aigc": self.is_aigc,
        }


class InstagramRequest(BaseModel):
    format: str = "9:16"
    caption: str = Field(default="", max_length=2200)
    share_to_feed: bool = True

    @field_validator("format")
    @classmethod
    def _format(cls, v: str) -> str:
        return get_format(v).key

    @field_validator("caption")
    @classmethod
    def _caption(cls, v: str) -> str:
        if v.count("#") > 30:
            raise ValueError("Instagram allows at most 30 hashtags.")
        return v.strip()


def _guard(
    db: Session,
    p: VideoProject,
    s: VideoSettings,
    platform: str,
    fmt: str,
    confirmed_by: str,
    confirmed: bool,
) -> Path:
    if not available(s)[platform]:
        raise UploadDisabled(
            f"{platform.title()} upload is off. Set RB_VIDEO_UPLOAD_ENABLED=true and the "
            f"{platform.title()} credentials (see the README)."
        )
    if p.status != "approved":
        raise ValueError("Only approved videos can be uploaded.")
    if not confirmed or not confirmed_by.strip():
        raise ValueError("Confirm that you reviewed this render before uploading.")
    existing = db.scalar(
        select(Upload).where(
            Upload.project_id == p.id,
            Upload.platform == platform,
            Upload.format == fmt,
            Upload.status.not_in(("failed", "expired")),
        )
    )
    if existing:
        raise ValueError(f"The {fmt} render was already sent to {platform.title()}.")
    return render_file(p, fmt, s)


def _rotate_writer(s: VideoSettings):
    """Keep TikTok's rotated refresh token in the token file (never the database)."""
    if not s.tiktok_token_file:
        return lambda _new: log.warning(
            "TikTok issued a new refresh token; set RB_VIDEO_TIKTOK_TOKEN_FILE to keep it, or "
            "re-authorize before the current one expires."
        )

    def write(new: str) -> None:
        path = Path(s.tiktok_token_file)
        tmp = path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(new)
        os.replace(tmp, path)

    return write


def tiktok_client(s: VideoSettings, http: httpx.Client | None = None) -> clients.TikTok:
    return clients.TikTok(s.tiktok_credentials, http, on_rotate=_rotate_writer(s))


def instagram_client(s: VideoSettings, http: httpx.Client | None = None) -> clients.Instagram:
    return clients.Instagram(
        s.instagram_credentials, s.instagram_graph_host, s.instagram_api_version, http
    )


def tiktok_options(s: VideoSettings, http: httpx.Client | None = None) -> dict:
    """Direct mode: the account's allowed privacy options and settings, which TikTok requires
    apps to show before posting (reading this posts nothing)."""
    info = tiktok_client(s, http).creator_info()
    return {
        "nickname": info.get("creator_nickname", ""),
        "username": info.get("creator_username", ""),
        "privacy": [o for o in info.get("privacy_level_options", []) if o in TIKTOK_PRIVACY],
        "max_seconds": info.get("max_video_post_duration_sec"),
        "comment_disabled": bool(info.get("comment_disabled")),
        "duet_disabled": bool(info.get("duet_disabled")),
        "stitch_disabled": bool(info.get("stitch_disabled")),
    }


def upload_tiktok(
    db: Session,
    p: VideoProject,
    req: TikTokRequest,
    s: VideoSettings,
    *,
    confirmed_by: str,
    confirmed: bool,
    confirmed_public: bool = False,
    http: httpx.Client | None = None,
) -> Upload:
    path = _guard(db, p, s, "tiktok", req.format, confirmed_by, confirmed)
    tk = tiktok_client(s, http)
    meta: dict = {}
    if s.tiktok_mode == "direct":
        opts = tiktok_options(s, http)
        if req.privacy is None:
            raise ValueError("Choose who can view this TikTok.")
        if req.privacy not in opts["privacy"]:
            raise ValueError("That privacy option isn't available for this TikTok account.")
        if req.privacy != "SELF_ONLY" and not confirmed_public:
            raise ValueError("Confirm that other people will be able to see this TikTok.")
        seconds = media_duration(path) or 0
        if opts["max_seconds"] and seconds > opts["max_seconds"]:
            raise ValueError(f"TikTok allows at most {opts['max_seconds']} s for this account.")
        post = req.post_info()
        post["disable_comment"] |= opts["comment_disabled"]
        post["disable_duet"] |= opts["duet_disabled"]
        post["disable_stitch"] |= opts["stitch_disabled"]
        publish_id = tk.upload(path, post)
        meta["username"] = opts["username"]
    else:
        publish_id = tk.upload(path)  # lands in the creator's TikTok inbox as a draft
    up = Upload(
        project_id=p.id,
        platform="tiktok",
        format=req.format,
        mode=s.tiktok_mode,
        status="processing",
        external_ref=publish_id,
        privacy=req.privacy if s.tiktok_mode == "direct" else None,
        uploaded_by=confirmed_by[:200],
        meta=meta,
    )
    db.add(up)
    db.flush()
    log.info("tiktok %s upload for project %s by %s", s.tiktok_mode, p.id, confirmed_by)
    return up


def upload_instagram(
    db: Session,
    p: VideoProject,
    req: InstagramRequest,
    s: VideoSettings,
    *,
    confirmed_by: str,
    confirmed: bool,
    http: httpx.Client | None = None,
) -> Upload:
    """Step 1: upload into a container. Nothing is public until :func:`publish_instagram`."""
    path = _guard(db, p, s, "instagram", req.format, confirmed_by, confirmed)
    ig = instagram_client(s, http)
    cid = ig.create_reel(req.caption, req.share_to_feed)
    ig.send_file(cid, path)
    up = Upload(
        project_id=p.id,
        platform="instagram",
        format=req.format,
        mode="reel",
        status="processing",
        external_ref=cid,
        privacy="public",
        uploaded_by=confirmed_by[:200],
        meta={},
    )
    db.add(up)
    db.flush()
    log.info("instagram reel container %s for project %s by %s", cid, p.id, confirmed_by)
    return up


def _record(db: Session, up: Upload, url: str) -> None:
    try:
        url = performance.classify(url, up.platform)[1]  # same form as publications
    except ValueError:
        log.warning("%s returned an unexpected post URL for upload %s", up.platform, up.id)
        return
    up.url = url
    project = db.get(VideoProject, up.project_id)
    try:
        pub = performance.record(db, project, url, platform=up.platform, format=up.format)
        pub.uploaded_by, pub.privacy = up.published_by or up.uploaded_by, up.privacy
    except ValueError as exc:  # already recorded by hand: keep the upload's URL anyway
        log.info("not recording %s: %s", url, exc)


def refresh(db: Session, up: Upload, s: VideoSettings, http: httpx.Client | None = None) -> Upload:
    """Read the platform's status for one upload. Never publishes anything."""
    if up.status not in PENDING:
        return up
    if up.platform == "tiktok":
        data = tiktok_client(s, http).status(up.external_ref)
        state = data.get("status", "")
        if state == "FAILED":
            up.status, up.error = "failed", str(data.get("fail_reason", ""))[:500]
        elif state == "SEND_TO_USER_INBOX":
            up.status = "in_inbox"
        elif state == "PUBLISH_COMPLETE":
            up.status = "published"
            ids = data.get("publicaly_available_post_id") or []  # (sic) TikTok's spelling
            user = up.meta.get("username") or s.tiktok_username
            if ids and user:
                _record(db, up, f"https://www.tiktok.com/@{user}/video/{ids[0]}")
    elif up.platform == "instagram":
        data = instagram_client(s, http).status(up.external_ref)
        code = data.get("status_code", "")
        if code == "FINISHED":
            up.status = "ready"
        elif code == "ERROR":
            up.status, up.error = "failed", str(data.get("status", ""))[:500]
        elif code == "EXPIRED":
            up.status = "expired"
        elif code == "PUBLISHED":
            up.status = "published"
    up.updated_at = utcnow()
    return up


def refresh_pending(db: Session, s: VideoSettings, http: httpx.Client | None = None) -> list[str]:
    """Status check for every pending upload (used by the tracking job; read-only)."""
    notes = []
    for up in db.scalars(select(Upload).where(Upload.status.in_(PENDING))):
        try:
            refresh(db, up, s, http)
        except (clients.MissingKey, clients.PlatformError, httpx.HTTPError, ValueError) as exc:
            notes.append(f"{up.platform} upload #{up.id}: {exc}")
    return notes


def publish_instagram(
    db: Session,
    up: Upload,
    s: VideoSettings,
    *,
    confirmed_by: str,
    confirmed: bool,
    http: httpx.Client | None = None,
) -> Upload:
    """Step 2: a person makes the Reel public. The only function that calls media_publish."""
    if up.platform != "instagram":
        raise ValueError("Only Instagram uploads are published this way.")
    if not available(s)["instagram"]:
        raise UploadDisabled("Instagram upload is off.")
    if not confirmed or not confirmed_by.strip():
        raise ValueError("Confirm that this Reel should be public on Instagram now.")
    refresh(db, up, s, http)
    if up.status != "ready":
        raise ValueError(
            {
                "processing": "Instagram is still processing the video; try again shortly.",
                "published": "This Reel is already published.",
                "expired": "The upload expired (24 hours); upload it again.",
            }.get(up.status, f"Can't publish: {up.error or up.status}.")
        )
    ig = instagram_client(s, http)
    media_id = ig.publish(up.external_ref)
    up.status, up.published_by = "published", confirmed_by[:200]
    up.meta = {**up.meta, "media_id": media_id}
    link = ig.permalink(media_id)
    if link:
        _record(db, up, link)
    log.info("instagram reel %s published by %s", media_id, confirmed_by)
    return up
