"""Person-confirmed uploads to TikTok, Instagram, Facebook and LinkedIn (YouTube is in
:mod:`upload`).

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
- **Facebook Page Reels:** saved as a draft on the Page by default (publish it in Meta
  Business Suite); publishing right away needs a second confirmation.
- **LinkedIn:** step 1 uploads the video (not visible); step 2 is a separate click that
  creates the post, with the visibility (anyone / connections) chosen then.
- **X:** step 1 uploads the video (not posted); step 2 is a separate click that posts it.
- **Pinterest:** step 1 uploads the video (not visible); step 2 is a separate click that
  creates the Pin on the configured board.
- **Reddit:** step 1 uploads the video and a poster frame to Reddit's media storage (not
  posted); step 2 is a separate click where the person picks one subreddit and a title.
  Each render can go to one subreddit only, so nothing is cross-posted in bulk.
- **Bluesky:** step 1 uploads to Bluesky's video service; step 2 is a separate click.
- **Tumblr:** one request creates the post with the video, as a draft by default (publish
  it from Tumblr), private, or published right away with a second confirmation.
- **Vimeo:** one upload with the privacy chosen up front: only me (default), anyone with
  the link, or anyone (both need a second confirmation).
- **Threads:** step 1 lets Threads fetch the render from a signed link valid for one hour
  (it needs this site's public https address); step 2 is a separate click that posts it.

:func:`refresh` only *reads* status. It's safe to run from the tracking job, and it never
publishes: that happens only in the ``publish_*`` functions, each behind a person's click.
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import logging
import os
import time
from datetime import timedelta
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

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

NAMES = {
    "youtube": "YouTube",
    "tiktok": "TikTok",
    "instagram": "Instagram",
    "facebook": "Facebook",
    "linkedin": "LinkedIn",
    "x": "X",
    "threads": "Threads",
    "pinterest": "Pinterest",
    "reddit": "Reddit",
    "bluesky": "Bluesky",
    "tumblr": "Tumblr",
    "vimeo": "Vimeo",
}


def name(platform: str) -> str:
    return NAMES.get(platform, platform.title())


PENDING = ("processing", "in_inbox", "ready", "draft")
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
    "PUBLIC": "Anyone",
    "CONNECTIONS": "Connections",
    "board": "On your board",
    "nobody": "Only me",
    "unlisted": "Anyone with the link",
    "anybody": "Anyone",
}


def available(s: VideoSettings) -> dict[str, bool]:
    return {
        "tiktok": s.upload_enabled and s.tiktok_credentials is not None,
        "instagram": s.upload_enabled and s.instagram_credentials is not None,
        "facebook": s.upload_enabled and s.facebook_credentials is not None,
        "linkedin": s.upload_enabled and s.linkedin_credentials is not None,
        "x": s.upload_enabled and s.x_credentials is not None,
        "threads": s.upload_enabled and s.threads_credentials is not None,
        "reddit": (
            s.upload_enabled
            and s.reddit_credentials is not None
            and bool(s.reddit_post_refresh_token and s.reddit_username)
        ),
        "bluesky": s.upload_enabled and s.bluesky_credentials is not None,
        "tumblr": s.upload_enabled and s.tumblr_credentials is not None and bool(s.tumblr_blog),
        "vimeo": s.upload_enabled and s.vimeo_access_token is not None,
        "pinterest": (
            s.upload_enabled and s.pinterest_credentials is not None and bool(s.pinterest_board_id)
        ),
    }


def _caption_default(p: VideoProject, limit: int) -> str:
    script = Script.model_validate(p.script) if p.script else None
    parts = [script.title if script else p.topic]
    if p.ai_generated:
        parts.append("Made with AI assistance and reviewed by a human.")
    if script and script.hashtags:
        parts.append(" ".join("#" + h.lstrip("#") for h in script.hashtags[:10]))
    return "\n\n".join(parts)[:limit]


def _short_caption(p: VideoProject, limit: int) -> str:
    """Title, hashtags and a short AI note, trimmed to fit a short-post limit."""
    script = Script.model_validate(p.script) if p.script else None
    title = script.title if script else p.topic
    tags = " ".join("#" + h.lstrip("#") for h in (script.hashtags[:3] if script else []))
    note = "(AI-assisted)" if p.ai_generated else ""
    for parts in ([title, tags, note], [title, note], [title]):
        text = " ".join(x for x in parts if x)
        if len(text) <= limit:
            return text
    return title[: limit - 1] + "…"


def defaults(p: VideoProject) -> dict:
    script = Script.model_validate(p.script) if p.script else None
    return {
        "tiktok_caption": _caption_default(p, 2200),
        "instagram_caption": _caption_default(p, 2200),
        "facebook_description": _caption_default(p, 5000),
        "linkedin_commentary": _caption_default(p, 3000),
        "x_text": _short_caption(p, 280),
        "threads_text": _short_caption(p, 500),
        "pinterest_description": _caption_default(p, 800),
        "bluesky_text": _short_caption(p, 300),
        "tumblr_caption": _caption_default(p, 4000),
        "tumblr_tags": ", ".join(
            h.lstrip("#") for h in (Script.model_validate(p.script).hashtags if p.script else [])
        ),
        "title": (script.title if script else p.topic)[:200],
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


class FacebookRequest(BaseModel):
    format: str = "9:16"
    title: str = Field(default="", max_length=255)
    description: str = Field(default="", max_length=5000)
    publish_now: bool = False  # default: saved as a draft on the Page

    @field_validator("format")
    @classmethod
    def _format(cls, v: str) -> str:
        return get_format(v).key


class LinkedInRequest(BaseModel):
    format: str = "16:9"
    title: str = Field(default="", max_length=200)
    commentary: str = Field(default="", max_length=3000)

    @field_validator("format")
    @classmethod
    def _format(cls, v: str) -> str:
        return get_format(v).key


class XRequest(BaseModel):
    format: str = "16:9"
    text: str = Field(default="", max_length=25000)

    @field_validator("format")
    @classmethod
    def _format(cls, v: str) -> str:
        return get_format(v).key


class PinterestRequest(BaseModel):
    format: str = "9:16"
    title: str = Field(default="", max_length=100)
    description: str = Field(default="", max_length=800)
    alt_text: str = Field(default="", max_length=500)
    link: str = Field(default="", max_length=2048)

    @field_validator("format")
    @classmethod
    def _format(cls, v: str) -> str:
        return get_format(v).key

    @field_validator("link")
    @classmethod
    def _link(cls, v: str) -> str:
        v = v.strip()
        parts = urlsplit(v)
        if v and (parts.scheme != "https" or not parts.hostname or parts.username):
            raise ValueError("The Pin link must be an https:// address.")
        return v


class BlueskyRequest(BaseModel):
    format: str = "9:16"
    text: str = Field(default="", max_length=300)

    @field_validator("format")
    @classmethod
    def _format(cls, v: str) -> str:
        return get_format(v).key


class TumblrRequest(BaseModel):
    format: str = "9:16"
    caption: str = Field(default="", max_length=4096)
    tags: list[str] = Field(default_factory=list)
    state: Literal["draft", "private", "published"] = "draft"

    @field_validator("format")
    @classmethod
    def _format(cls, v: str) -> str:
        return get_format(v).key

    @field_validator("tags", mode="before")
    @classmethod
    def _tags(cls, v) -> list[str]:
        items = v.split(",") if isinstance(v, str) else list(v or [])
        tags = [str(t).strip().lstrip("#")[:140] for t in items]
        return list(dict.fromkeys(t for t in tags if t))[:30]


class VimeoRequest(BaseModel):
    format: str = "16:9"
    title: str = Field(default="", max_length=128)
    description: str = Field(default="", max_length=5000)
    privacy: Literal["nobody", "unlisted", "anybody"] = "nobody"

    @field_validator("format")
    @classmethod
    def _format(cls, v: str) -> str:
        return get_format(v).key


class ThreadsRequest(BaseModel):
    format: str = "9:16"
    text: str = Field(default="", max_length=500)

    @field_validator("format")
    @classmethod
    def _format(cls, v: str) -> str:
        return get_format(v).key


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
            f"{name(platform)} upload is off. Set RB_VIDEO_UPLOAD_ENABLED=true and the "
            f"{name(platform)} credentials (see the README)."
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
        raise ValueError(f"The {fmt} render was already sent to {name(platform)}.")
    return render_file(p, fmt, s)


def _write_token(path: Path, token: str) -> None:
    """Atomically replace a token file, readable only by this user (never the database)."""
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(token)
    os.replace(tmp, path)


def _rotate_writer(s: VideoSettings):
    """Keep TikTok's rotated refresh token in the token file."""
    if not s.tiktok_token_file:
        return lambda _new: log.warning(
            "TikTok issued a new refresh token; set RB_VIDEO_TIKTOK_TOKEN_FILE to keep it, or "
            "re-authorize before the current one expires."
        )
    return lambda new: _write_token(Path(s.tiktok_token_file), new)


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


def facebook_client(s: VideoSettings, http: httpx.Client | None = None) -> clients.FacebookPage:
    return clients.FacebookPage(s.facebook_credentials, s.facebook_api_version, http)


def linkedin_client(s: VideoSettings, http: httpx.Client | None = None) -> clients.LinkedIn:
    return clients.LinkedIn(s.linkedin_credentials, s.linkedin_version, http)


def upload_facebook(
    db: Session,
    p: VideoProject,
    req: FacebookRequest,
    s: VideoSettings,
    *,
    confirmed_by: str,
    confirmed: bool,
    confirmed_public: bool = False,
    http: httpx.Client | None = None,
) -> Upload:
    """Upload a Page Reel: as a draft (default; publish it in Meta Business Suite) or, with a
    second confirmation, published right away."""
    path = _guard(db, p, s, "facebook", req.format, confirmed_by, confirmed)
    if req.publish_now and not confirmed_public:
        raise ValueError("Confirm that the Reel should be public on your Page right away.")
    video_id = facebook_client(s, http).upload(
        path,
        description=req.description,
        title=req.title,
        state="PUBLISHED" if req.publish_now else "DRAFT",
    )
    up = Upload(
        project_id=p.id,
        platform="facebook",
        format=req.format,
        mode="publish" if req.publish_now else "draft",
        status="processing",
        external_ref=video_id,
        privacy="public",
        uploaded_by=confirmed_by[:200],
        published_by=confirmed_by[:200] if req.publish_now else None,
        meta={},
    )
    db.add(up)
    db.flush()
    log.info("facebook reel %s (%s) for project %s by %s", video_id, up.mode, p.id, confirmed_by)
    return up


def upload_linkedin(
    db: Session,
    p: VideoProject,
    req: LinkedInRequest,
    s: VideoSettings,
    *,
    confirmed_by: str,
    confirmed: bool,
    http: httpx.Client | None = None,
) -> Upload:
    """Step 1: upload the video (not visible). The post is created by :func:`publish_linkedin`."""
    path = _guard(db, p, s, "linkedin", req.format, confirmed_by, confirmed)
    video = linkedin_client(s, http).upload(path)
    up = Upload(
        project_id=p.id,
        platform="linkedin",
        format=req.format,
        mode="post",
        status="processing",
        external_ref=video,
        uploaded_by=confirmed_by[:200],
        meta={"title": req.title or p.topic[:200], "commentary": req.commentary},
    )
    db.add(up)
    db.flush()
    log.info("linkedin video %s for project %s by %s", video, p.id, confirmed_by)
    return up


def _refresh_facebook(db: Session, up: Upload, s: VideoSettings, http) -> None:
    st = facebook_client(s, http).status(up.external_ref)
    phases = [st.get(k) or {} for k in ("uploading_phase", "processing_phase", "publishing_phase")]
    if st.get("video_status") == "error" or any(ph.get("status") == "error" for ph in phases):
        errors = [e for ph in phases for e in ph.get("errors", [])]
        up.status = "failed"
        up.error = str(errors[0].get("message") if errors else "Facebook reported an error")[:500]
    elif phases[2].get("publish_status") == "published":
        up.status = "published"
        _record(db, up, f"https://www.facebook.com/reel/{up.external_ref}")
    elif up.mode == "draft" and st.get("video_status") == "ready":
        up.status = "draft"  # waiting for a person to publish it in Meta Business Suite


def _refresh_linkedin(up: Upload, s: VideoSettings, http) -> None:
    st = linkedin_client(s, http).video_status(up.external_ref)
    state = st.get("status", "")
    if state == "AVAILABLE":
        up.status = "ready"
    elif state == "PROCESSING_FAILED":
        up.status = "failed"
        up.error = str(st.get("processingFailureReason", "processing failed"))[:500]


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
    elif up.platform == "facebook":
        _refresh_facebook(db, up, s, http)
    elif up.platform == "linkedin":
        _refresh_linkedin(up, s, http)
    elif up.platform == "x":
        state, error = x_client(s, http).media_state(up.external_ref)
        if state == "succeeded":
            up.status = "ready"
        elif state == "failed":
            up.status, up.error = "failed", (error or "X couldn't process the video")[:500]
    elif up.platform == "pinterest":
        state = pinterest_client(s, http).media_status(up.external_ref)
        if state == "succeeded":
            up.status = "ready"
        elif state == "failed":
            up.status, up.error = "failed", "Pinterest couldn't process the video"
    elif up.platform == "bluesky":
        job = bluesky_client(s, http).job_status(up.external_ref)
        state = job.get("state", "")
        if state == "JOB_STATE_COMPLETED" and job.get("blob"):
            up.status, up.meta = "ready", {**up.meta, "blob": job["blob"]}
        elif state == "JOB_STATE_FAILED":
            up.status = "failed"
            up.error = str(job.get("error") or job.get("message") or "processing failed")[:500]
    elif up.platform == "reddit" and up.status == "processing":
        since = float(up.meta.get("submitted_at") or 0)
        url = reddit_client(s, http).find_post(up.meta["subreddit"], up.meta["title"], since)
        if url:
            up.status = "published"
            _record(db, up, url)
    elif up.platform == "tumblr" and up.status == "draft":
        if tumblr_client(s, http).post_state(up.external_ref) == "published":
            up.status = "published"
            _record(db, up, tumblr_post_url(s, up.external_ref))
    elif up.platform == "vimeo":
        st = vimeo_client(s, http).status(up.external_ref)
        transcode = (st.get("transcode") or {}).get("status", "")
        view = (st.get("privacy") or {}).get("view", "")
        if transcode == "error":
            up.status, up.error = "failed", "Vimeo couldn't transcode the video"
        elif transcode == "complete":
            up.privacy = view or up.privacy
            if view in ("anybody", "unlisted"):
                up.status = "published"
                _record(db, up, str(st.get("link") or up.meta.get("link", "")))
            else:
                up.status = "draft"  # only visible to the owner until they change it
    elif up.platform == "threads" and up.external_ref:
        data = threads_client(s, http).status(up.external_ref)
        code = data.get("status", "")
        if code == "FINISHED":
            up.status = "ready"
        elif code == "ERROR":
            up.status = "failed"
            up.error = str(data.get("error_message") or "Threads couldn't use the video")[:500]
        elif code == "EXPIRED":
            up.status = "expired"
        elif code == "PUBLISHED":
            up.status = "published"
    up.updated_at = utcnow()
    return up


def refresh_pending(db: Session, s: VideoSettings, http: httpx.Client | None = None) -> list[str]:
    """Status check for every pending upload (used by the tracking job; read-only)."""
    notes = []
    since = utcnow() - timedelta(days=s.track_days)
    query = select(Upload).where(Upload.status.in_(PENDING), Upload.created_at >= since)
    for up in db.scalars(query):
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


def publish_linkedin(
    db: Session,
    up: Upload,
    s: VideoSettings,
    *,
    visibility: str | None,
    confirmed_by: str,
    confirmed: bool,
    http: httpx.Client | None = None,
) -> Upload:
    """Step 2: a person creates the LinkedIn post. The only function that posts there."""
    if up.platform != "linkedin":
        raise ValueError("Only LinkedIn uploads are published this way.")
    if not available(s)["linkedin"]:
        raise UploadDisabled("LinkedIn upload is off.")
    if visibility not in ("PUBLIC", "CONNECTIONS"):
        raise ValueError("Choose who can see the LinkedIn post.")
    if visibility == "CONNECTIONS" and ":organization:" in (s.linkedin_author_urn or ""):
        raise ValueError("Company pages can only post publicly.")
    if not confirmed or not confirmed_by.strip():
        raise ValueError("Confirm that this should be posted on LinkedIn now.")
    refresh(db, up, s, http)
    if up.status != "ready":
        raise ValueError(
            {
                "processing": "LinkedIn is still processing the video; try again shortly.",
                "published": "This video is already posted.",
            }.get(up.status, f"Can't post: {up.error or up.status}.")
        )
    urn = linkedin_client(s, http).post(
        video=up.external_ref,
        commentary=up.meta.get("commentary", ""),
        title=up.meta.get("title", ""),
        visibility=visibility,
    )
    up.status, up.published_by, up.privacy = "published", confirmed_by[:200], visibility
    up.meta = {**up.meta, "post": urn}
    _record(db, up, f"https://www.linkedin.com/feed/update/{urn}")
    log.info("linkedin post %s by %s", urn, confirmed_by)
    return up


def publish(
    db: Session,
    up: Upload,
    s: VideoSettings,
    *,
    confirmed_by: str,
    confirmed: bool,
    visibility: str | None = None,
    subreddit: str = "",
    title: str = "",
    nsfw: bool = False,
    http: httpx.Client | None = None,
) -> Upload:
    """Step 2 for platforms that upload first and publish on a separate click."""
    if up.platform == "reddit":
        return publish_reddit(
            db,
            up,
            s,
            subreddit=subreddit,
            title=title,
            nsfw=nsfw,
            confirmed_by=confirmed_by,
            confirmed=confirmed,
            http=http,
        )
    if up.platform == "instagram":
        return publish_instagram(
            db, up, s, confirmed_by=confirmed_by, confirmed=confirmed, http=http
        )
    if up.platform == "linkedin":
        return publish_linkedin(
            db,
            up,
            s,
            visibility=visibility,
            confirmed_by=confirmed_by,
            confirmed=confirmed,
            http=http,
        )
    if up.platform in ("x", "threads", "pinterest", "bluesky"):
        fn = {
            "x": publish_x,
            "threads": publish_threads,
            "pinterest": publish_pinterest,
            "bluesky": publish_bluesky,
        }[up.platform]
        return fn(db, up, s, confirmed_by=confirmed_by, confirmed=confirmed, http=http)
    raise ValueError(f"{name(up.platform)} uploads aren't published from here.")


# ---------------------------------------------------------------------- X


def x_client(s: VideoSettings, http: httpx.Client | None = None) -> clients.XClient:
    path = Path(s.x_token_file) if s.x_token_file else None
    return clients.XClient(
        s.x_credentials, http, on_rotate=(lambda new: _write_token(path, new)) if path else None
    )


def upload_x(
    db: Session,
    p: VideoProject,
    req: XRequest,
    s: VideoSettings,
    *,
    confirmed_by: str,
    confirmed: bool,
    http: httpx.Client | None = None,
) -> Upload:
    """Step 1: upload the video to X (not posted). :func:`publish_x` creates the post."""
    path = _guard(db, p, s, "x", req.format, confirmed_by, confirmed)
    if len(req.text) > s.x_max_chars:
        raise ValueError(f"X posts are limited to {s.x_max_chars} characters here.")
    media_id, state = x_client(s, http).upload(path)
    up = Upload(
        project_id=p.id,
        platform="x",
        format=req.format,
        mode="post",
        status="ready" if state == "succeeded" else "processing",
        external_ref=media_id,
        uploaded_by=confirmed_by[:200],
        meta={"text": req.text},
    )
    db.add(up)
    db.flush()
    log.info("x media %s for project %s by %s", media_id, p.id, confirmed_by)
    return up


def publish_x(
    db: Session,
    up: Upload,
    s: VideoSettings,
    *,
    confirmed_by: str,
    confirmed: bool,
    http: httpx.Client | None = None,
) -> Upload:
    """Step 2: a person creates the X post. The only function that posts on X."""
    if up.platform != "x":
        raise ValueError("Only X uploads are published this way.")
    if not available(s)["x"]:
        raise UploadDisabled("X upload is off.")
    if not confirmed or not confirmed_by.strip():
        raise ValueError("Confirm that this should be posted on X now.")
    refresh(db, up, s, http)
    if up.status != "ready":
        raise ValueError(
            {
                "processing": "X is still processing the video; try again shortly.",
                "published": "This video is already posted.",
            }.get(up.status, f"Can't post: {up.error or up.status}.")
        )
    post_id = x_client(s, http).post(up.meta.get("text", ""), up.external_ref)
    up.status, up.published_by, up.privacy = "published", confirmed_by[:200], "public"
    up.meta = {**up.meta, "post": post_id}
    _record(db, up, f"https://x.com/i/status/{post_id}")
    log.info("x post %s by %s", post_id, confirmed_by)
    return up


# ---------------------------------------------------------------------- Threads

MEDIA_LINK_TTL = 3600


def _media_sig(secret: str, pid: int, slug: str, expires: int) -> str:
    msg = f"video-media:{pid}:{slug}:{expires}".encode()
    return hmac.new(secret.encode(), msg, hashlib.sha256).hexdigest()[:40]


def media_link(public_base: str, secret: str, pid: int, fmt: str, expires: int) -> str:
    slug = get_format(fmt).slug
    sig = _media_sig(secret, pid, slug, expires)
    return f"{public_base.rstrip('/')}/video-media/{pid}/{slug}/{expires}/{sig}.mp4"


def verify_media_link(secret: str, pid: int, slug: str, expires: int, sig: str) -> bool:
    if expires < time.time() or expires > time.time() + MEDIA_LINK_TTL + 60:
        return False
    return hmac.compare_digest(_media_sig(secret, pid, slug, expires), sig)


def public_base_problem(url: str) -> str | None:
    """Why Meta couldn't fetch videos from this address, or None if it looks public."""
    parts = urlsplit(url or "")
    host = (parts.hostname or "").lower()
    if parts.scheme != "https" or not host:
        return "Threads needs this site's public https address (RB_VIDEO_PUBLIC_BASE_URL)."
    if host == "localhost" or host.endswith((".local", ".internal", ".localhost")):
        return "Threads can't reach a local address; set RB_VIDEO_PUBLIC_BASE_URL."
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return None
    if not ip.is_global:
        return "Threads can't reach a private address; set RB_VIDEO_PUBLIC_BASE_URL."
    return None


def threads_client(s: VideoSettings, http: httpx.Client | None = None) -> clients.Threads:
    return clients.Threads(s.threads_credentials, http)


def upload_threads(
    db: Session,
    p: VideoProject,
    req: ThreadsRequest,
    s: VideoSettings,
    *,
    public_base: str,
    secret: str,
    confirmed_by: str,
    confirmed: bool,
    http: httpx.Client | None = None,
) -> Upload:
    """Step 1: Threads fetches the render from a signed link valid for one hour (for this
    project's render only) into a container. Nothing is public until
    :func:`publish_threads`."""
    _guard(db, p, s, "threads", req.format, confirmed_by, confirmed)
    problem = public_base_problem(public_base)
    if problem:
        raise ValueError(problem)
    expires = int(time.time()) + MEDIA_LINK_TTL
    cid = threads_client(s, http).create_video(
        media_link(public_base, secret, p.id, req.format, expires), req.text
    )
    up = Upload(
        project_id=p.id,
        platform="threads",
        format=req.format,
        mode="post",
        status="processing",
        external_ref=cid,
        privacy="public",
        uploaded_by=confirmed_by[:200],
        meta={"text": req.text, "media_expires": expires},
    )
    db.add(up)
    db.flush()
    log.info("threads container %s for project %s by %s", cid, p.id, confirmed_by)
    return up


def media_for_link(db: Session, s: VideoSettings, pid: int, slug: str) -> Path | None:
    """The render a valid media link points to (approved projects only)."""
    project = db.get(VideoProject, pid)
    if project is None or project.status != "approved":
        return None
    try:
        return render_file(project, slug, s)
    except ValueError:
        return None


def publish_threads(
    db: Session,
    up: Upload,
    s: VideoSettings,
    *,
    confirmed_by: str,
    confirmed: bool,
    http: httpx.Client | None = None,
) -> Upload:
    """Step 2: a person publishes the Threads post. The only function that calls
    threads_publish."""
    if up.platform != "threads":
        raise ValueError("Only Threads uploads are published this way.")
    if not available(s)["threads"]:
        raise UploadDisabled("Threads upload is off.")
    if not confirmed or not confirmed_by.strip():
        raise ValueError("Confirm that this should be posted on Threads now.")
    refresh(db, up, s, http)
    if up.status != "ready":
        raise ValueError(
            {
                "processing": "Threads is still processing the video; try again shortly.",
                "published": "This video is already posted.",
                "expired": "The upload expired (24 hours); upload it again.",
            }.get(up.status, f"Can't post: {up.error or up.status}.")
        )
    th = threads_client(s, http)
    media_id = th.publish(up.external_ref)
    up.status, up.published_by = "published", confirmed_by[:200]
    up.meta = {**up.meta, "media_id": media_id}
    link = th.permalink(media_id)
    if link:
        _record(db, up, link)
    log.info("threads post %s by %s", media_id, confirmed_by)
    return up


# ---------------------------------------------------------------------- Pinterest


def pinterest_client(s: VideoSettings, http: httpx.Client | None = None) -> clients.Pinterest:
    path = Path(s.pinterest_token_file) if s.pinterest_token_file else None

    def keep(new: str) -> None:
        if path:
            _write_token(path, new)
        else:
            log.warning(
                "Pinterest issued a new refresh token; set RB_VIDEO_PINTEREST_TOKEN_FILE to keep "
                "it, or re-authorize before the current one expires."
            )

    return clients.Pinterest(
        s.pinterest_credentials, s.pinterest_board_id, s.pinterest_api_host, http, on_rotate=keep
    )


def upload_pinterest(
    db: Session,
    p: VideoProject,
    req: PinterestRequest,
    s: VideoSettings,
    *,
    confirmed_by: str,
    confirmed: bool,
    http: httpx.Client | None = None,
) -> Upload:
    """Step 1: upload the video to Pinterest (not visible). :func:`publish_pinterest` pins it."""
    path = _guard(db, p, s, "pinterest", req.format, confirmed_by, confirmed)
    media_id = pinterest_client(s, http).upload(path)
    up = Upload(
        project_id=p.id,
        platform="pinterest",
        format=req.format,
        mode="pin",
        status="processing",
        external_ref=media_id,
        uploaded_by=confirmed_by[:200],
        meta={
            "title": req.title or p.topic[:100],
            "description": req.description,
            "alt_text": req.alt_text,
            "link": req.link,
        },
    )
    db.add(up)
    db.flush()
    log.info("pinterest media %s for project %s by %s", media_id, p.id, confirmed_by)
    return up


def publish_pinterest(
    db: Session,
    up: Upload,
    s: VideoSettings,
    *,
    confirmed_by: str,
    confirmed: bool,
    http: httpx.Client | None = None,
) -> Upload:
    """Step 2: a person creates the Pin. The only function that creates Pins."""
    if up.platform != "pinterest":
        raise ValueError("Only Pinterest uploads are published this way.")
    if not available(s)["pinterest"]:
        raise UploadDisabled("Pinterest upload is off.")
    if not confirmed or not confirmed_by.strip():
        raise ValueError("Confirm that this should be pinned on Pinterest now.")
    refresh(db, up, s, http)
    if up.status != "ready":
        raise ValueError(
            {
                "processing": "Pinterest is still processing the video; try again shortly.",
                "published": "This video is already pinned.",
            }.get(up.status, f"Can't pin: {up.error or up.status}.")
        )
    pin_id = pinterest_client(s, http).create_pin(
        media_id=up.external_ref,
        title=up.meta.get("title", ""),
        description=up.meta.get("description", ""),
        link=up.meta.get("link", ""),
        alt_text=up.meta.get("alt_text", ""),
    )
    up.status, up.published_by, up.privacy = "published", confirmed_by[:200], "board"
    up.meta = {**up.meta, "pin": pin_id}
    _record(db, up, f"https://www.pinterest.com/pin/{pin_id}")
    log.info("pinterest pin %s by %s", pin_id, confirmed_by)
    return up


# ---------------------------------------------------------------------- Reddit


def reddit_client(s: VideoSettings, http: httpx.Client | None = None) -> clients.RedditPoster:
    token = s.reddit_post_refresh_token.get_secret_value() if s.reddit_post_refresh_token else None
    return clients.RedditPoster(
        s.reddit_credentials, token, s.reddit_username, s.reddit_user_agent, http
    )


def upload_reddit(
    db: Session,
    p: VideoProject,
    fmt: str,
    s: VideoSettings,
    *,
    confirmed_by: str,
    confirmed: bool,
    http: httpx.Client | None = None,
) -> Upload:
    """Step 1: upload the video and a poster frame to Reddit's media storage (not posted)."""
    from redblue.video.render import poster_frame

    fmt = get_format(fmt).key
    path = _guard(db, p, s, "reddit", fmt, confirmed_by, confirmed)
    poster = poster_frame(path, path.with_suffix(".poster.jpg"))
    rd = reddit_client(s, http)
    video_url = rd.upload_asset(path, "video/mp4")
    poster_url = rd.upload_asset(poster, "image/jpeg")
    up = Upload(
        project_id=p.id,
        platform="reddit",
        format=fmt,
        mode="post",
        status="ready",
        external_ref=f"project-{p.id}-{get_format(fmt).slug}",
        privacy="public",
        uploaded_by=confirmed_by[:200],
        meta={"video_url": video_url, "poster_url": poster_url},
    )
    db.add(up)
    db.flush()
    log.info("reddit media for project %s by %s", p.id, confirmed_by)
    return up


def publish_reddit(
    db: Session,
    up: Upload,
    s: VideoSettings,
    *,
    subreddit: str,
    title: str,
    nsfw: bool = False,
    confirmed_by: str,
    confirmed: bool,
    http: httpx.Client | None = None,
) -> Upload:
    """Step 2: a person posts it to one subreddit they chose. The only function that
    submits to Reddit; an upload can be submitted once."""
    if up.platform != "reddit":
        raise ValueError("Only Reddit uploads are published this way.")
    if not available(s)["reddit"]:
        raise UploadDisabled("Reddit upload is off.")
    subreddit = subreddit.strip().removeprefix("r/").removeprefix("/r/")
    title = title.strip()
    if not clients.RedditPoster.SUBREDDIT.fullmatch(subreddit):
        raise ValueError("Enter one subreddit name, like technology (without r/).")
    if not 1 <= len(title) <= 300:
        raise ValueError("Reddit titles are 1 to 300 characters.")
    if not confirmed or not confirmed_by.strip():
        raise ValueError(
            "Confirm that you've read r/" + subreddit + "'s rules and want to post there now."
        )
    if up.status != "ready":
        raise ValueError(
            "This video was already posted to Reddit."
            if up.status in ("processing", "published")
            else f"Can't post: {up.error or up.status}."
        )
    reddit_client(s, http).submit_video(
        subreddit=subreddit,
        title=title,
        video_url=up.meta["video_url"],
        poster_url=up.meta["poster_url"],
        nsfw=nsfw,
    )
    up.status, up.published_by = "processing", confirmed_by[:200]
    up.meta = {**up.meta, "subreddit": subreddit, "title": title, "submitted_at": time.time()}
    db.flush()
    try:  # Reddit creates video posts asynchronously; the link usually appears quickly
        refresh(db, up, s, http)
    except (clients.PlatformError, httpx.HTTPError) as exc:
        log.info("reddit post lookup deferred: %s", exc)
    log.info("reddit post to r/%s by %s", subreddit, confirmed_by)
    return up


# ---------------------------------------------------------------------- Bluesky


def bluesky_client(s: VideoSettings, http: httpx.Client | None = None) -> clients.Bluesky:
    return clients.Bluesky(s.bluesky_credentials, s.bluesky_pds, http)


def upload_bluesky(
    db: Session,
    p: VideoProject,
    req: BlueskyRequest,
    s: VideoSettings,
    *,
    confirmed_by: str,
    confirmed: bool,
    http: httpx.Client | None = None,
) -> Upload:
    """Step 1: upload to Bluesky's video service (not posted). :func:`publish_bluesky` posts."""
    path = _guard(db, p, s, "bluesky", req.format, confirmed_by, confirmed)
    job_id = bluesky_client(s, http).upload_video(path)
    up = Upload(
        project_id=p.id,
        platform="bluesky",
        format=req.format,
        mode="post",
        status="processing",
        external_ref=job_id[:200],
        privacy="public",
        uploaded_by=confirmed_by[:200],
        meta={"text": req.text},
    )
    db.add(up)
    db.flush()
    log.info("bluesky video job %s for project %s by %s", job_id, p.id, confirmed_by)
    return up


def publish_bluesky(
    db: Session,
    up: Upload,
    s: VideoSettings,
    *,
    confirmed_by: str,
    confirmed: bool,
    http: httpx.Client | None = None,
) -> Upload:
    """Step 2: a person creates the Bluesky post. The only function that posts there."""
    if up.platform != "bluesky":
        raise ValueError("Only Bluesky uploads are published this way.")
    if not available(s)["bluesky"]:
        raise UploadDisabled("Bluesky upload is off.")
    if not confirmed or not confirmed_by.strip():
        raise ValueError("Confirm that this should be posted on Bluesky now.")
    refresh(db, up, s, http)
    if up.status != "ready":
        raise ValueError(
            {
                "processing": "Bluesky is still processing the video; try again shortly.",
                "published": "This video is already posted.",
            }.get(up.status, f"Can't post: {up.error or up.status}.")
        )
    fmt = get_format(up.format)
    bs = bluesky_client(s, http)
    rkey = bs.post(
        text=up.meta.get("text", ""), blob=up.meta["blob"], width=fmt.width, height=fmt.height
    )
    up.status, up.published_by = "published", confirmed_by[:200]
    up.meta = {**up.meta, "post": rkey}
    handle = (s.bluesky_handle or "").lstrip("@")
    _record(db, up, f"https://bsky.app/profile/{handle}/post/{rkey}")
    log.info("bluesky post %s by %s", rkey, confirmed_by)
    return up


# ---------------------------------------------------------------------- Tumblr


def tumblr_client(s: VideoSettings, http: httpx.Client | None = None) -> clients.Tumblr:
    path = Path(s.tumblr_token_file) if s.tumblr_token_file else None

    def keep(new: str) -> None:
        if path:
            _write_token(path, new)
        else:
            log.warning(
                "Tumblr issued a new refresh token; set RB_VIDEO_TUMBLR_TOKEN_FILE to keep it, "
                "or the next refresh will need a new authorization."
            )

    return clients.Tumblr(s.tumblr_credentials, s.tumblr_blog, http, on_rotate=keep)


def tumblr_post_url(s: VideoSettings, post_id: str) -> str:
    return f"https://www.tumblr.com/{s.tumblr_blog}/{post_id}"


def upload_tumblr(
    db: Session,
    p: VideoProject,
    req: TumblrRequest,
    s: VideoSettings,
    *,
    confirmed_by: str,
    confirmed: bool,
    confirmed_public: bool = False,
    http: httpx.Client | None = None,
) -> Upload:
    """Create a Tumblr post with the video: a draft by default (the person publishes it from
    Tumblr), private, or published right away with a second confirmation."""
    path = _guard(db, p, s, "tumblr", req.format, confirmed_by, confirmed)
    if req.state == "published" and not confirmed_public:
        raise ValueError("Confirm that the post should be public on Tumblr right away.")
    fmt = get_format(req.format)
    post_id = tumblr_client(s, http).create_video_post(
        path,
        caption=req.caption,
        tags=req.tags,
        state=req.state,
        width=fmt.width,
        height=fmt.height,
    )
    up = Upload(
        project_id=p.id,
        platform="tumblr",
        format=req.format,
        mode=req.state,
        status="draft" if req.state == "draft" else "published",
        external_ref=post_id,
        privacy="private" if req.state == "private" else "public",
        uploaded_by=confirmed_by[:200],
        published_by=confirmed_by[:200] if req.state == "published" else None,
        meta={"blog": s.tumblr_blog},
    )
    db.add(up)
    db.flush()
    if req.state == "published":
        _record(db, up, tumblr_post_url(s, post_id))
    log.info("tumblr %s post %s for project %s by %s", req.state, post_id, p.id, confirmed_by)
    return up


# ---------------------------------------------------------------------- Vimeo


def vimeo_client(s: VideoSettings, http: httpx.Client | None = None) -> clients.Vimeo:
    token = s.vimeo_access_token.get_secret_value() if s.vimeo_access_token else None
    return clients.Vimeo(token, http)


def upload_vimeo(
    db: Session,
    p: VideoProject,
    req: VimeoRequest,
    s: VideoSettings,
    *,
    confirmed_by: str,
    confirmed: bool,
    confirmed_public: bool = False,
    http: httpx.Client | None = None,
) -> Upload:
    """Upload to Vimeo with the privacy the person chose (only me by default). Anyone with
    the link, or anyone, needs a second confirmation."""
    path = _guard(db, p, s, "vimeo", req.format, confirmed_by, confirmed)
    if req.privacy != "nobody" and not confirmed_public:
        raise ValueError("Confirm that other people will be able to watch it on Vimeo.")
    script = Script.model_validate(p.script) if p.script else None
    video_id, link = vimeo_client(s, http).upload(
        path,
        name=req.title or (script.title if script else p.topic)[:128],
        description=req.description,
        privacy=req.privacy,
    )
    up = Upload(
        project_id=p.id,
        platform="vimeo",
        format=req.format,
        mode=req.privacy,
        status="processing",
        external_ref=video_id,
        privacy=req.privacy,
        uploaded_by=confirmed_by[:200],
        published_by=confirmed_by[:200] if req.privacy != "nobody" else None,
        meta={"link": link},
    )
    db.add(up)
    db.flush()
    log.info("vimeo video %s (%s) for project %s by %s", video_id, req.privacy, p.id, confirmed_by)
    return up
