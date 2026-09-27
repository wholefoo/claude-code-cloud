"""Optional direct upload of an approved render to YouTube, always confirmed by a person.

Guardrails (the platform's "humans approve anything public" rule):

- Off by default: needs ``RB_VIDEO_UPLOAD_ENABLED=true`` *and* an upload token.
- Only approved projects, and every call needs an explicit confirmation from the person
  doing it (the admin form's checkbox, or the CLI's interactive prompt). Nothing schedules
  or chains an upload: no job, sweep or pipeline step calls this module.
- Private by default. Public needs a second confirmation. "Made for kids" has no default:
  the person must choose. AI narration is disclosed as synthetic media by default.
- One upload per project and format, recorded as a publication (so tracking starts).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Literal

import httpx
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from redblue.video import clients, performance
from redblue.video.config import VideoSettings
from redblue.video.models import Publication, VideoProject
from redblue.video.pipeline import description
from redblue.video.schemas import Script
from redblue.video.templates import get_format

log = logging.getLogger("redblue.video.upload")


class UploadDisabled(RuntimeError):
    pass


def _clean(text: str) -> str:
    # YouTube rejects "<" and ">" in titles and descriptions.
    return text.replace("<", "‹").replace(">", "›").strip()


class UploadRequest(BaseModel):
    format: str
    title: str = Field(min_length=1, max_length=100)
    description: str = ""
    tags: list[str] = Field(default_factory=list)
    privacy: Literal["private", "unlisted", "public"] = "private"
    made_for_kids: bool  # no default: a person must answer
    synthetic_media: bool = True
    category_id: str = "28"

    @field_validator("format")
    @classmethod
    def _format(cls, v: str) -> str:
        return get_format(v).key

    @field_validator("title", mode="before")
    @classmethod
    def _title(cls, v: str) -> str:
        return _clean(str(v))

    @field_validator("description")
    @classmethod
    def _description(cls, v: str) -> str:
        v = _clean(v)
        if len(v.encode()) > 5000:
            raise ValueError("The description must be under 5,000 bytes.")
        return v

    @field_validator("tags", mode="before")
    @classmethod
    def _tags(cls, v) -> list[str]:
        items = v.split(",") if isinstance(v, str) else list(v or [])
        tags = [_clean(str(t)).lstrip("#")[:100] for t in items]
        tags = list(dict.fromkeys(t for t in tags if t))
        while len(",".join(tags)) > 450:  # YouTube caps tags at ~500 characters in total
            tags.pop()
        return tags

    @field_validator("category_id")
    @classmethod
    def _category(cls, v: str) -> str:
        if not v.isdigit():
            raise ValueError("Category id must be a number.")
        return v

    def resource(self) -> dict:
        return {
            "snippet": {
                "title": self.title,
                "description": self.description,
                "tags": self.tags,
                "categoryId": self.category_id,
            },
            "status": {
                "privacyStatus": self.privacy,
                "selfDeclaredMadeForKids": self.made_for_kids,
                "containsSyntheticMedia": self.synthetic_media,
            },
        }


def enabled(s: VideoSettings) -> bool:
    return s.upload_enabled and s.youtube_upload_oauth is not None


def render_file(p: VideoProject, fmt: str, s: VideoSettings) -> Path:
    """The rendered file for ``fmt``, only if it's inside the output directory."""
    key = get_format(fmt).key
    raw = (p.renders or {}).get(key)
    if not p.renders and key == "9:16":  # projects rendered before multiple formats
        raw = p.render_path
    path = Path(raw or "").resolve()
    if not raw or s.output_dir.resolve() not in path.parents or not path.is_file():
        raise ValueError(f"There's no {key} render for this project.")
    return path


def defaults(p: VideoProject, s: VideoSettings) -> dict:
    """Pre-filled form values (a person reviews and edits them before uploading)."""
    script = Script.model_validate(p.script) if p.script else None
    return {
        "title": _clean(script.title if script else p.topic)[:100],
        "description": _clean(description(p))[:4900] if p.script else "",
        "tags": ", ".join(h.lstrip("#") for h in (script.hashtags if script else [])),
        "category_id": s.upload_category_id,
        "synthetic_media": True,
    }


def already_uploaded(db: Session, p: VideoProject, fmt: str) -> Publication | None:
    return db.scalar(
        select(Publication).where(
            Publication.project_id == p.id,
            Publication.platform == "youtube",
            Publication.format == get_format(fmt).key,
            Publication.uploaded_by.is_not(None),
        )
    )


def upload(
    db: Session,
    p: VideoProject,
    req: UploadRequest,
    s: VideoSettings,
    *,
    confirmed_by: str,
    confirmed: bool,
    confirmed_public: bool = False,
    http: httpx.Client | None = None,
) -> Publication:
    """Upload one approved render to YouTube. ``confirmed_by`` names the person."""
    if not enabled(s):
        raise UploadDisabled(
            "Direct upload is off. Set RB_VIDEO_UPLOAD_ENABLED=true and the YouTube upload "
            "credentials (see the README)."
        )
    if p.status != "approved":
        raise ValueError("Only approved videos can be uploaded.")
    if not confirmed or not confirmed_by.strip():
        raise ValueError("Confirm that you reviewed this render before uploading.")
    if req.privacy == "public" and not confirmed_public:
        raise ValueError("Confirm that the video should be public as soon as it's uploaded.")
    if already_uploaded(db, p, req.format):
        raise ValueError(f"The {req.format} render was already uploaded to YouTube.")
    path = render_file(p, req.format, s)

    body = clients.YouTubeUploader(s.youtube_upload_oauth, http).upload(path, req.resource())
    video_id = body["id"]
    privacy = body.get("status", {}).get("privacyStatus") or req.privacy
    log.info("uploaded project %s (%s) as %s by %s", p.id, req.format, video_id, confirmed_by)
    url = f"https://www.youtube.com/watch?v={video_id}"
    try:
        pub = performance.record(db, p, url, platform="youtube", format=req.format)
    except ValueError as exc:  # uploaded, but e.g. the URL was already recorded by hand
        raise ValueError(f"Uploaded as {url}, but couldn't record it: {exc}") from exc
    pub.uploaded_by, pub.privacy = confirmed_by[:200], privacy
    return pub
