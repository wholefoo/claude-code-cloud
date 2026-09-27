"""Admin pages for the video pipeline: ranked trends, projects, script editing, preview and
human approval. Mounted under /admin/video by :func:`install_video`."""

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Annotated

import httpx
from fastapi import APIRouter, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import ValidationError
from sqlalchemy import select

from redblue.admin.app import DB, Writer, back, need
from redblue.admin.app import HERE as ADMIN_DIR
from redblue.core.auth import Role
from redblue.templates.env import make_templates
from redblue.video import performance
from redblue.video import upload as uploads
from redblue.video import upload_social as social
from redblue.video.config import get_video_settings
from redblue.video.models import MetricSnapshot, Publication, Trend, Upload, VideoProject
from redblue.video.pipeline import Pipeline, description
from redblue.video.schemas import Script
from redblue.video.templates import FORMATS, TEMPLATES, get_format

templates = make_templates([Path(__file__).parent / "templates", ADMIN_DIR / "templates"])
router = APIRouter(prefix="/admin/video", include_in_schema=False)
# Public, unauthenticated: only the short-lived signed Threads media links.
public_router = APIRouter(include_in_schema=False)


def _pipeline(request: Request) -> Pipeline:
    return Pipeline(request.app.state.rb, get_video_settings())


def _render(request: Request, name: str, user, **ctx) -> HTMLResponse:
    from redblue.cms.collections import COLLECTIONS

    rb = request.app.state.rb
    ctx.update(
        request=request,
        user=user,
        settings=rb.settings,
        Role=Role,
        csrf_token=getattr(request.state, "csrf_token", ""),
        nonce=getattr(request.state, "csp_nonce", ""),
        collections=COLLECTIONS,
        flash=request.query_params.get("msg", ""),
        vs=get_video_settings(),
    )
    return templates.TemplateResponse(request, f"admin/{name}", ctx)


def _project(db, pid: int) -> VideoProject:
    p = db.get(VideoProject, pid)
    if p is None:
        raise HTTPException(404)
    return p


@router.get("")
def index(request: Request, db: DB, user: Writer) -> HTMLResponse:
    trends = list(
        db.scalars(
            select(Trend).where(Trend.status == "new").order_by(Trend.score.desc()).limit(40)
        )
    )
    projects = list(
        db.scalars(select(VideoProject).order_by(VideoProject.updated_at.desc()).limit(30))
    )
    vs = get_video_settings()
    keys = {k: bool(vs.key(k)) for k in ("youtube", "tavily", "firecrawl", "pexels", "elevenlabs")}
    keys["reddit"] = bool(vs.reddit_credentials and vs.subreddits)
    keys["google_trends"] = True  # public RSS feed, no key
    return _render(request, "video_index.html", user, trends=trends, projects=projects, keys=keys)


@router.post("/sweep")
def sweep(request: Request, db: DB, user: Writer):
    added = _pipeline(request).sweep(db)
    return back(
        "/admin/video",
        f"Sweep found {len(added)} new trend(s)."
        if added
        else "No new trends (check API keys in the status panel).",
    )


@router.post("/trends/{tid}/dismiss")
def dismiss(tid: int, db: DB, user: Writer):
    t = db.get(Trend, tid)
    if t:
        t.status = "dismissed"
    return back("/admin/video")


@router.post("/projects")
def create(
    request: Request,
    db: DB,
    user: Writer,
    trend_id: Annotated[int, Form()] = 0,
    topic: Annotated[str, Form()] = "",
):
    trend = db.get(Trend, trend_id) if trend_id else None
    try:
        p = _pipeline(request).start_project(db, trend=trend, topic=topic or None)
    except ValueError as exc:
        return back("/admin/video", str(exc))
    return back(
        f"/admin/video/projects/{p.id}", "Project created. Run research to build the brief."
    )


@router.get("/projects/{pid:int}")
def project(pid: int, request: Request, db: DB, user: Writer) -> HTMLResponse:
    p = _project(db, pid)
    vs = get_video_settings()
    tpl, fmts = _pipeline(request).look(p)
    renders = [
        {
            "key": k,
            "slug": get_format(k).slug,
            "label": get_format(k).label,
            "width": get_format(k).width,
            "height": get_format(k).height,
        }
        for k in (p.renders or ({"9:16": p.render_path} if p.render_path else {}))
    ]
    return _render(
        request,
        "video_project.html",
        user,
        project=p,
        script_json=json.dumps(p.script, indent=2) if p.script else "",
        upload_text=description(p) if p.script else "",
        templates_list=list(TEMPLATES.values()),
        formats_list=list(FORMATS.values()),
        current_template=tpl.key,
        current_formats=[f.key for f in fmts],
        renders=renders,
        publications=_publications(db, pid),
        platforms=list(performance.PLATFORMS),
        upload_enabled=uploads.enabled(vs),
        upload_defaults=uploads.defaults(p, vs) if p.status == "approved" else {},
        uploaded={
            r["key"] for r in renders if uploads.already_uploaded(db, p, r["key"]) is not None
        },
        social=social.available(vs),
        social_defaults=social.defaults(p) if p.status == "approved" else {},
        tiktok_mode=vs.tiktok_mode,
        tiktok_opts=_tiktok_options(vs) if _wants_tiktok_options(p, vs, user) else None,
        privacy_labels=social.PRIVACY_LABELS,
        threads_problem=social.public_base_problem(_public_base(request)),
        x_max_chars=vs.x_max_chars,
        social_uploads=list(
            db.scalars(select(Upload).where(Upload.project_id == pid).order_by(Upload.id))
        ),
    )


def _public_base(request: Request) -> str:
    return get_video_settings().public_base_url or request.app.state.rb.settings.base_url


def _wants_tiktok_options(p: VideoProject, vs, user) -> bool:
    return (
        p.status == "approved"
        and vs.tiktok_mode == "direct"
        and social.available(vs)["tiktok"]
        and user.has_role(Role.editor)
    )


def _tiktok_options(vs) -> dict:
    """TikTok requires showing the account's own options before a direct post."""
    try:
        return social.tiktok_options(vs)
    except (social.clients.PlatformError, social.clients.MissingKey, httpx.HTTPError) as exc:
        return {"error": str(exc)[:200]}


def _publications(db, pid: int) -> list[dict]:
    pubs = db.scalars(
        select(Publication).where(Publication.project_id == pid).order_by(Publication.id)
    )
    out = []
    for pub in pubs:
        latest = db.scalar(
            select(MetricSnapshot)
            .where(MetricSnapshot.publication_id == pub.id)
            .order_by(MetricSnapshot.taken_at.desc())
            .limit(1)
        )
        out.append({"pub": pub, "latest": latest})
    return out


def _date(value: str) -> datetime | None:
    if not value.strip():
        return None
    try:
        return datetime.fromisoformat(value.strip())
    except ValueError:
        raise ValueError("Enter the publish date as YYYY-MM-DD.") from None


def _count(value: str) -> int | None:
    value = value.strip().replace(",", "")
    if not value:
        return None
    if not value.isdigit():
        raise ValueError("Counts must be whole numbers.")
    return int(value)


@router.post("/projects/{pid:int}/publications")
def add_publication(
    pid: int,
    db: DB,
    user: Writer,
    url: Annotated[str, Form()],
    platform: Annotated[str, Form()] = "",
    format: Annotated[str, Form()] = "",
    published_on: Annotated[str, Form()] = "",
):
    p = _project(db, pid)
    try:
        pub = performance.record(
            db,
            p,
            url,
            platform=platform or None,
            format=format or None,
            published_at=_date(published_on),
        )
    except ValueError as exc:
        return back(f"/admin/video/projects/{pid}", str(exc))
    hint = (
        " Stats are fetched every 6 hours."
        if pub.platform == "youtube"
        else " Add its numbers by hand or import a CSV on the Performance page."
    )
    return back(f"/admin/video/projects/{pid}", f"Recorded on {pub.platform}.{hint}")


def _publication(db, pub_id: int) -> Publication:
    pub = db.get(Publication, pub_id)
    if pub is None:
        raise HTTPException(404)
    return pub


@router.post("/projects/{pid:int}/upload")
def upload_video(
    pid: int,
    db: DB,
    user: Writer,
    format: Annotated[str, Form()],
    title: Annotated[str, Form()],
    made_for_kids: Annotated[str, Form()] = "",
    description: Annotated[str, Form()] = "",
    tags: Annotated[str, Form()] = "",
    privacy: Annotated[str, Form()] = "private",
    synthetic_media: Annotated[str, Form()] = "",
    confirm: Annotated[str, Form()] = "",
    confirm_public: Annotated[str, Form()] = "",
    shorts: Annotated[str, Form()] = "",
):
    """A person presses Upload: the only way a video leaves RedBlue from the admin."""
    need(user, Role.editor)
    p = _project(db, pid)
    dest = f"/admin/video/projects/{pid}"
    if made_for_kids not in ("yes", "no"):
        return back(dest, 'Answer "Made for kids?" before uploading.')
    vs = get_video_settings()
    try:
        req = uploads.UploadRequest(
            format=format,
            title=title,
            description=description,
            tags=tags,
            privacy=privacy,
            made_for_kids=made_for_kids == "yes",
            synthetic_media=bool(synthetic_media),
            category_id=vs.upload_category_id,
            shorts=bool(shorts),
        )
        pub = uploads.upload(
            db,
            p,
            req,
            vs,
            confirmed_by=user.email,
            confirmed=bool(confirm),
            confirmed_public=bool(confirm_public),
        )
    except ValidationError as exc:
        return back(dest, f"Check the upload form: {exc.errors()[0]['msg']}"[:300])
    except (ValueError, uploads.UploadDisabled, httpx.HTTPError) as exc:
        return back(dest, f"Upload failed: {exc}"[:300])
    return back(dest, f"Uploaded to YouTube as {pub.privacy}: {pub.url}")


def _flag(value: str) -> bool:
    return value in ("1", "on", "true", "yes")


@router.post("/projects/{pid:int}/tiktok")
def upload_tiktok(
    pid: int,
    db: DB,
    user: Writer,
    format: Annotated[str, Form()] = "9:16",
    caption: Annotated[str, Form()] = "",
    privacy: Annotated[str, Form()] = "",
    allow_comments: Annotated[str, Form()] = "",
    allow_duet: Annotated[str, Form()] = "",
    allow_stitch: Annotated[str, Form()] = "",
    is_aigc: Annotated[str, Form()] = "",
    brand_organic: Annotated[str, Form()] = "",
    brand_content: Annotated[str, Form()] = "",
    confirm: Annotated[str, Form()] = "",
    confirm_public: Annotated[str, Form()] = "",
):
    need(user, Role.editor)
    p = _project(db, pid)
    dest = f"/admin/video/projects/{pid}"
    vs = get_video_settings()
    try:
        req = social.TikTokRequest(
            format=format,
            caption=caption,
            privacy=privacy or None,
            allow_comments=_flag(allow_comments),
            allow_duet=_flag(allow_duet),
            allow_stitch=_flag(allow_stitch),
            is_aigc=_flag(is_aigc),
            brand_organic=_flag(brand_organic),
            brand_content=_flag(brand_content),
        )
        up = social.upload_tiktok(
            db,
            p,
            req,
            vs,
            confirmed_by=user.email,
            confirmed=_flag(confirm),
            confirmed_public=_flag(confirm_public),
        )
    except ValidationError as exc:
        return back(dest, f"Check the TikTok form: {exc.errors()[0]['msg']}"[:300])
    except (ValueError, uploads.UploadDisabled, social.clients.PlatformError) as exc:
        return back(dest, f"TikTok upload failed: {exc}"[:300])
    except httpx.HTTPError as exc:
        return back(dest, f"TikTok upload failed: {exc}"[:300])
    msg = (
        "Sent to TikTok. Open the TikTok app (inbox notification) to finish and post it."
        if up.mode == "inbox"
        else "Sent to TikTok; it's processing. Check status in a minute."
    )
    return back(dest, msg)


@router.post("/projects/{pid:int}/instagram")
def upload_instagram(
    pid: int,
    db: DB,
    user: Writer,
    format: Annotated[str, Form()] = "9:16",
    caption: Annotated[str, Form()] = "",
    share_to_feed: Annotated[str, Form()] = "",
    confirm: Annotated[str, Form()] = "",
):
    need(user, Role.editor)
    p = _project(db, pid)
    dest = f"/admin/video/projects/{pid}"
    try:
        req = social.InstagramRequest(
            format=format, caption=caption, share_to_feed=_flag(share_to_feed)
        )
        social.upload_instagram(
            db, p, req, get_video_settings(), confirmed_by=user.email, confirmed=_flag(confirm)
        )
    except ValidationError as exc:
        return back(dest, f"Check the Instagram form: {exc.errors()[0]['msg']}"[:300])
    except (ValueError, uploads.UploadDisabled, social.clients.PlatformError) as exc:
        return back(dest, f"Instagram upload failed: {exc}"[:300])
    except httpx.HTTPError as exc:
        return back(dest, f"Instagram upload failed: {exc}"[:300])
    return back(
        dest,
        "Uploaded to Instagram (not public yet). When it's processed, press Publish below.",
    )


def _upload(db, uid: int) -> Upload:
    up = db.get(Upload, uid)
    if up is None:
        raise HTTPException(404)
    return up


@router.post("/uploads/{uid:int}/refresh")
def refresh_upload(uid: int, db: DB, user: Writer):
    up = _upload(db, uid)
    dest = f"/admin/video/projects/{up.project_id}"
    try:
        social.refresh(db, up, get_video_settings())
    except (social.clients.MissingKey, social.clients.PlatformError, httpx.HTTPError) as exc:
        return back(dest, f"Status check failed: {exc}"[:300])
    return back(dest, f"{social.name(up.platform)} upload: {up.status.replace('_', ' ')}.")


@router.post("/uploads/{uid:int}/publish")
def publish_upload(
    uid: int,
    db: DB,
    user: Writer,
    confirm: Annotated[str, Form()] = "",
    visibility: Annotated[str, Form()] = "",
    subreddit: Annotated[str, Form()] = "",
    title: Annotated[str, Form()] = "",
    nsfw: Annotated[str, Form()] = "",
):
    """Step 2 (upload-then-post platforms): a person makes it public."""
    need(user, Role.editor)
    up = _upload(db, uid)
    dest = f"/admin/video/projects/{up.project_id}"
    try:
        social.publish(
            db,
            up,
            get_video_settings(),
            confirmed_by=user.email,
            confirmed=_flag(confirm),
            visibility=visibility or None,
            subreddit=subreddit,
            title=title,
            nsfw=_flag(nsfw),
        )
    except (ValueError, uploads.UploadDisabled, social.clients.PlatformError) as exc:
        return back(dest, f"Publish failed: {exc}"[:300])
    except httpx.HTTPError as exc:
        return back(dest, f"Publish failed: {exc}"[:300])
    where = social.name(up.platform)
    return back(dest, f"Published on {where}{': ' + up.url if up.url else ''}.")


@router.post("/projects/{pid:int}/x")
def upload_x(
    pid: int,
    db: DB,
    user: Writer,
    format: Annotated[str, Form()] = "16:9",
    text: Annotated[str, Form()] = "",
    confirm: Annotated[str, Form()] = "",
):
    need(user, Role.editor)
    p = _project(db, pid)
    dest = f"/admin/video/projects/{pid}"
    try:
        req = social.XRequest(format=format, text=text)
        social.upload_x(
            db, p, req, get_video_settings(), confirmed_by=user.email, confirmed=_flag(confirm)
        )
    except ValidationError as exc:
        return back(dest, f"Check the X form: {exc.errors()[0]['msg']}"[:300])
    except (ValueError, uploads.UploadDisabled, social.clients.PlatformError) as exc:
        return back(dest, f"X upload failed: {exc}"[:300])
    except httpx.HTTPError as exc:
        return back(dest, f"X upload failed: {exc}"[:300])
    return back(dest, "Uploaded to X (not posted yet). When it's processed, press Post below.")


@router.post("/projects/{pid:int}/reddit")
def upload_reddit(
    pid: int,
    db: DB,
    user: Writer,
    format: Annotated[str, Form()] = "9:16",
    confirm: Annotated[str, Form()] = "",
):
    need(user, Role.editor)
    p = _project(db, pid)
    dest = f"/admin/video/projects/{pid}"
    try:
        social.upload_reddit(
            db, p, format, get_video_settings(), confirmed_by=user.email, confirmed=_flag(confirm)
        )
    except (ValueError, uploads.UploadDisabled, social.clients.PlatformError) as exc:
        return back(dest, f"Reddit upload failed: {exc}"[:300])
    except httpx.HTTPError as exc:
        return back(dest, f"Reddit upload failed: {exc}"[:300])
    return back(dest, "Uploaded to Reddit (not posted yet). Choose a subreddit below to post it.")


@router.post("/projects/{pid:int}/bluesky")
def upload_bluesky(
    pid: int,
    db: DB,
    user: Writer,
    format: Annotated[str, Form()] = "9:16",
    text: Annotated[str, Form()] = "",
    confirm: Annotated[str, Form()] = "",
):
    need(user, Role.editor)
    p = _project(db, pid)
    dest = f"/admin/video/projects/{pid}"
    try:
        req = social.BlueskyRequest(format=format, text=text)
        social.upload_bluesky(
            db, p, req, get_video_settings(), confirmed_by=user.email, confirmed=_flag(confirm)
        )
    except ValidationError as exc:
        return back(dest, f"Check the Bluesky form: {exc.errors()[0]['msg']}"[:300])
    except (ValueError, uploads.UploadDisabled, social.clients.PlatformError) as exc:
        return back(dest, f"Bluesky upload failed: {exc}"[:300])
    except httpx.HTTPError as exc:
        return back(dest, f"Bluesky upload failed: {exc}"[:300])
    return back(
        dest, "Uploaded to Bluesky (not posted yet). When it's processed, press Post below."
    )


@router.post("/projects/{pid:int}/tumblr")
def upload_tumblr(
    pid: int,
    db: DB,
    user: Writer,
    format: Annotated[str, Form()] = "9:16",
    caption: Annotated[str, Form()] = "",
    tags: Annotated[str, Form()] = "",
    state: Annotated[str, Form()] = "draft",
    confirm: Annotated[str, Form()] = "",
    confirm_public: Annotated[str, Form()] = "",
):
    need(user, Role.editor)
    p = _project(db, pid)
    dest = f"/admin/video/projects/{pid}"
    try:
        req = social.TumblrRequest(format=format, caption=caption, tags=tags, state=state)
        up = social.upload_tumblr(
            db,
            p,
            req,
            get_video_settings(),
            confirmed_by=user.email,
            confirmed=_flag(confirm),
            confirmed_public=_flag(confirm_public),
        )
    except ValidationError as exc:
        return back(dest, f"Check the Tumblr form: {exc.errors()[0]['msg']}"[:300])
    except (ValueError, uploads.UploadDisabled, social.clients.PlatformError) as exc:
        return back(dest, f"Tumblr upload failed: {exc}"[:300])
    except httpx.HTTPError as exc:
        return back(dest, f"Tumblr upload failed: {exc}"[:300])
    messages = {
        "draft": "Saved as a Tumblr draft. Publish it from your Tumblr drafts.",
        "private": "Posted privately on Tumblr (only you can see it).",
        "published": f"Published on Tumblr: {up.url or ''}",
    }
    return back(dest, messages[up.mode])


@router.post("/projects/{pid:int}/vimeo")
def upload_vimeo(
    pid: int,
    db: DB,
    user: Writer,
    format: Annotated[str, Form()] = "16:9",
    title: Annotated[str, Form()] = "",
    description: Annotated[str, Form()] = "",
    privacy: Annotated[str, Form()] = "nobody",
    confirm: Annotated[str, Form()] = "",
    confirm_public: Annotated[str, Form()] = "",
):
    need(user, Role.editor)
    p = _project(db, pid)
    dest = f"/admin/video/projects/{pid}"
    try:
        req = social.VimeoRequest(
            format=format, title=title, description=description, privacy=privacy
        )
        social.upload_vimeo(
            db,
            p,
            req,
            get_video_settings(),
            confirmed_by=user.email,
            confirmed=_flag(confirm),
            confirmed_public=_flag(confirm_public),
        )
    except ValidationError as exc:
        return back(dest, f"Check the Vimeo form: {exc.errors()[0]['msg']}"[:300])
    except (ValueError, uploads.UploadDisabled, social.clients.PlatformError) as exc:
        return back(dest, f"Vimeo upload failed: {exc}"[:300])
    except httpx.HTTPError as exc:
        return back(dest, f"Vimeo upload failed: {exc}"[:300])
    return back(dest, "Uploaded to Vimeo; it's transcoding. Check status in a minute.")


@router.post("/projects/{pid:int}/dailymotion")
def upload_dailymotion(
    pid: int,
    db: DB,
    user: Writer,
    format: Annotated[str, Form()] = "16:9",
    title: Annotated[str, Form()] = "",
    description: Annotated[str, Form()] = "",
    tags: Annotated[str, Form()] = "",
    visibility: Annotated[str, Form()] = "draft",
    for_kids: Annotated[str, Form()] = "",
    confirm: Annotated[str, Form()] = "",
    confirm_public: Annotated[str, Form()] = "",
):
    need(user, Role.editor)
    p = _project(db, pid)
    dest = f"/admin/video/projects/{pid}"
    try:
        req = social.DailymotionRequest(
            format=format,
            title=title,
            description=description,
            tags=tags,
            visibility=visibility,
            for_kids=_flag(for_kids),
        )
        social.upload_dailymotion(
            db,
            p,
            req,
            get_video_settings(),
            confirmed_by=user.email,
            confirmed=_flag(confirm),
            confirmed_public=_flag(confirm_public),
        )
    except ValidationError as exc:
        return back(dest, f"Check the Dailymotion form: {exc.errors()[0]['msg']}"[:300])
    except (ValueError, uploads.UploadDisabled, social.clients.PlatformError) as exc:
        return back(dest, f"Dailymotion upload failed: {exc}"[:300])
    except httpx.HTTPError as exc:
        return back(dest, f"Dailymotion upload failed: {exc}"[:300])
    return back(dest, "Uploaded to Dailymotion; it's encoding. Check status in a few minutes.")


@router.post("/projects/{pid:int}/rumble")
def upload_rumble(
    pid: int,
    db: DB,
    user: Writer,
    format: Annotated[str, Form()] = "16:9",
    title: Annotated[str, Form()] = "",
    description: Annotated[str, Form()] = "",
    license: Annotated[str, Form()] = "none",
    confirm: Annotated[str, Form()] = "",
    confirm_public: Annotated[str, Form()] = "",
):
    need(user, Role.editor)
    p = _project(db, pid)
    dest = f"/admin/video/projects/{pid}"
    try:
        req = social.RumbleRequest(
            format=format, title=title, description=description, license=license
        )
        up = social.upload_rumble(
            db,
            p,
            req,
            get_video_settings(),
            confirmed_by=user.email,
            confirmed=_flag(confirm),
            confirmed_public=_flag(confirm_public),
        )
    except ValidationError as exc:
        return back(dest, f"Check the Rumble form: {exc.errors()[0]['msg']}"[:300])
    except (ValueError, uploads.UploadDisabled, social.clients.PlatformError) as exc:
        return back(dest, f"Rumble upload failed: {exc}"[:300])
    except httpx.HTTPError as exc:
        return back(dest, f"Rumble upload failed: {exc}"[:300])
    return back(dest, f"Published on Rumble: {up.url or 'link not returned yet'}")


@router.post("/projects/{pid:int}/pinterest")
def upload_pinterest(
    pid: int,
    db: DB,
    user: Writer,
    format: Annotated[str, Form()] = "9:16",
    title: Annotated[str, Form()] = "",
    description: Annotated[str, Form()] = "",
    alt_text: Annotated[str, Form()] = "",
    link: Annotated[str, Form()] = "",
    confirm: Annotated[str, Form()] = "",
):
    need(user, Role.editor)
    p = _project(db, pid)
    dest = f"/admin/video/projects/{pid}"
    try:
        req = social.PinterestRequest(
            format=format, title=title, description=description, alt_text=alt_text, link=link
        )
        social.upload_pinterest(
            db, p, req, get_video_settings(), confirmed_by=user.email, confirmed=_flag(confirm)
        )
    except ValidationError as exc:
        return back(dest, f"Check the Pinterest form: {exc.errors()[0]['msg']}"[:300])
    except (ValueError, uploads.UploadDisabled, social.clients.PlatformError) as exc:
        return back(dest, f"Pinterest upload failed: {exc}"[:300])
    except httpx.HTTPError as exc:
        return back(dest, f"Pinterest upload failed: {exc}"[:300])
    return back(
        dest, "Uploaded to Pinterest (not pinned yet). When it's processed, press Pin below."
    )


@router.post("/projects/{pid:int}/threads")
def upload_threads(
    pid: int,
    request: Request,
    db: DB,
    user: Writer,
    format: Annotated[str, Form()] = "9:16",
    text: Annotated[str, Form()] = "",
    confirm: Annotated[str, Form()] = "",
):
    need(user, Role.editor)
    p = _project(db, pid)
    dest = f"/admin/video/projects/{pid}"
    try:
        req = social.ThreadsRequest(format=format, text=text)
        social.upload_threads(
            db,
            p,
            req,
            get_video_settings(),
            public_base=_public_base(request),
            secret=request.app.state.rb.settings.secret_key.get_secret_value(),
            confirmed_by=user.email,
            confirmed=_flag(confirm),
        )
    except ValidationError as exc:
        return back(dest, f"Check the Threads form: {exc.errors()[0]['msg']}"[:300])
    except (ValueError, uploads.UploadDisabled, social.clients.PlatformError) as exc:
        return back(dest, f"Threads upload failed: {exc}"[:300])
    except httpx.HTTPError as exc:
        return back(dest, f"Threads upload failed: {exc}"[:300])
    return back(dest, "Sent to Threads (not posted yet). When it's processed, press Post below.")


@public_router.get("/video-media/{pid:int}/{slug}/{expires:int}/{sig}.mp4")
def threads_media(pid: int, slug: str, expires: int, sig: str, request: Request, db: DB):
    """One approved render, for Meta to fetch for Threads; the signed link lasts an hour."""
    secret = request.app.state.rb.settings.secret_key.get_secret_value()
    if not social.verify_media_link(secret, pid, slug, expires, sig):
        raise HTTPException(404)
    path = social.media_for_link(db, get_video_settings(), pid, slug)
    if path is None:
        raise HTTPException(404)
    return FileResponse(
        path,
        media_type="video/mp4",
        headers={"Cache-Control": "private, no-store", "X-Robots-Tag": "noindex"},
    )


@router.post("/projects/{pid:int}/facebook")
def upload_facebook(
    pid: int,
    db: DB,
    user: Writer,
    format: Annotated[str, Form()] = "9:16",
    title: Annotated[str, Form()] = "",
    description: Annotated[str, Form()] = "",
    publish_now: Annotated[str, Form()] = "",
    confirm: Annotated[str, Form()] = "",
    confirm_public: Annotated[str, Form()] = "",
):
    need(user, Role.editor)
    p = _project(db, pid)
    dest = f"/admin/video/projects/{pid}"
    try:
        req = social.FacebookRequest(
            format=format, title=title, description=description, publish_now=_flag(publish_now)
        )
        up = social.upload_facebook(
            db,
            p,
            req,
            get_video_settings(),
            confirmed_by=user.email,
            confirmed=_flag(confirm),
            confirmed_public=_flag(confirm_public),
        )
    except ValidationError as exc:
        return back(dest, f"Check the Facebook form: {exc.errors()[0]['msg']}"[:300])
    except (ValueError, uploads.UploadDisabled, social.clients.PlatformError) as exc:
        return back(dest, f"Facebook upload failed: {exc}"[:300])
    except httpx.HTTPError as exc:
        return back(dest, f"Facebook upload failed: {exc}"[:300])
    if up.mode == "draft":
        return back(dest, "Saved as a draft Reel on your Page. Publish it in Meta Business Suite.")
    return back(dest, "Sent to your Page; Facebook is processing it. Check status shortly.")


@router.post("/projects/{pid:int}/linkedin")
def upload_linkedin(
    pid: int,
    db: DB,
    user: Writer,
    format: Annotated[str, Form()] = "16:9",
    title: Annotated[str, Form()] = "",
    commentary: Annotated[str, Form()] = "",
    confirm: Annotated[str, Form()] = "",
):
    need(user, Role.editor)
    p = _project(db, pid)
    dest = f"/admin/video/projects/{pid}"
    try:
        req = social.LinkedInRequest(format=format, title=title, commentary=commentary)
        social.upload_linkedin(
            db, p, req, get_video_settings(), confirmed_by=user.email, confirmed=_flag(confirm)
        )
    except ValidationError as exc:
        return back(dest, f"Check the LinkedIn form: {exc.errors()[0]['msg']}"[:300])
    except (ValueError, uploads.UploadDisabled, social.clients.PlatformError) as exc:
        return back(dest, f"LinkedIn upload failed: {exc}"[:300])
    except httpx.HTTPError as exc:
        return back(dest, f"LinkedIn upload failed: {exc}"[:300])
    return back(
        dest, "Uploaded to LinkedIn (not posted yet). When it's processed, press Post below."
    )


@router.post("/publications/{pub_id:int}/metrics")
def add_metrics(
    pub_id: int,
    db: DB,
    user: Writer,
    views: Annotated[str, Form()],
    likes: Annotated[str, Form()] = "",
    comments: Annotated[str, Form()] = "",
    shares: Annotated[str, Form()] = "",
    avg_view_pct: Annotated[str, Form()] = "",
):
    pub = _publication(db, pub_id)
    dest = f"/admin/video/projects/{pub.project_id}"
    try:
        pct = avg_view_pct.strip().rstrip("%")
        performance.add_snapshot(
            db,
            pub,
            source="manual",
            views=_count(views) or 0,
            likes=_count(likes),
            comments=_count(comments),
            shares=_count(shares),
            avg_view_pct=float(pct) if pct else None,
        )
    except ValueError as exc:
        return back(dest, str(exc)[:200])
    return back(dest, "Numbers saved.")


@router.post("/publications/{pub_id:int}/delete")
def delete_publication(pub_id: int, db: DB, user: Writer):
    need(user, Role.editor)
    pub = _publication(db, pub_id)
    dest = f"/admin/video/projects/{pub.project_id}"
    db.delete(pub)
    return back(dest, "Publication removed.")


@router.get("/performance")
def performance_page(request: Request, db: DB, user: Writer) -> HTMLResponse:
    vs = get_video_settings()
    return _render(
        request,
        "video_performance.html",
        user,
        r=performance.report(db, vs),
        youtube_key=bool(vs.key("youtube")),
        youtube_oauth=bool(vs.youtube_oauth),
    )


@router.post("/performance/track")
def performance_track(request: Request, db: DB, user: Writer):
    res = _pipeline(request).track(db)
    msg = f"Updated {res.updated} video(s)."
    if res.notes:
        msg += " " + " ".join(res.notes)
    return back("/admin/video/performance", msg[:400])


@router.post("/performance/import")
async def performance_import(db: DB, user: Writer, file: Annotated[UploadFile, File()]):
    raw = (await file.read(2 * 1024 * 1024)).decode("utf-8-sig", errors="replace")
    n, errors = performance.import_csv(db, raw)
    msg = f"Imported {n} row(s)."
    if errors:
        msg += " " + "; ".join(errors[:5])
    return back("/admin/video/performance", msg[:400])


@router.post("/projects/{pid:int}/look")
def set_look(
    pid: int,
    request: Request,
    db: DB,
    user: Writer,
    template: Annotated[str, Form()],
    formats: Annotated[list[str] | None, Form()] = None,
):
    p = _project(db, pid)
    try:
        _pipeline(request).set_look(p, template, formats or [])
    except ValueError as exc:
        return back(f"/admin/video/projects/{pid}", str(exc))
    return back(f"/admin/video/projects/{pid}", "Look saved. Render to apply it.")


@router.post("/projects/{pid:int}/step")
def step(pid: int, request: Request, db: DB, user: Writer, action: Annotated[str, Form()]):
    p, pipe = _project(db, pid), _pipeline(request)
    try:
        match action:
            case "research":
                pipe.research(db, p)
                msg = "Brief ready. Check the sources."
            case "script":
                pipe.write_script(db, p)
                msg = "Script drafted." + (" Fix the problems listed." if p.problems else "")
            case "render":
                pipe.produce(db, p)
                msg = "Rendered. Watch it before approving."
            case _:
                raise HTTPException(400)
    except (ValueError, RuntimeError) as exc:
        return back(f"/admin/video/projects/{pid}", str(exc)[:300])
    return back(f"/admin/video/projects/{pid}", msg)


@router.post("/projects/{pid:int}/script")
def save_script(pid: int, request: Request, db: DB, user: Writer, script: Annotated[str, Form()]):
    p = _project(db, pid)
    try:
        problems = _pipeline(request).save_script(p, Script.model_validate_json(script))
    except (ValidationError, ValueError) as exc:
        return back(f"/admin/video/projects/{pid}", f"Invalid script: {str(exc)[:300]}")
    p.ai_generated = True  # edits keep the AI-assisted label; the human is the reviewer
    return back(
        f"/admin/video/projects/{pid}",
        "Script saved." + (" Problems: " + "; ".join(problems) if problems else ""),
    )


@router.post("/projects/{pid:int}/review")
def review(
    pid: int,
    request: Request,
    db: DB,
    user: Writer,
    decision: Annotated[str, Form()],
    note: Annotated[str, Form()] = "",
):
    need(user, Role.editor)
    try:
        _pipeline(request).review(_project(db, pid), user.id, decision == "approve", note)
    except ValueError as exc:
        return back(f"/admin/video/projects/{pid}", str(exc))
    return back(
        f"/admin/video/projects/{pid}",
        "Approved. Download it and upload it yourself: RedBlue never auto-posts."
        if decision == "approve"
        else "Rejected.",
    )


@router.get("/projects/{pid:int}/video.mp4")
def video_file(pid: int, db: DB, user: Writer, format: str = ""):
    p = _project(db, pid)
    if format:
        try:
            key = get_format(format).key
        except ValueError:
            raise HTTPException(404) from None
        raw = (p.renders or {}).get(key)
    else:
        raw, key = p.render_path, None
    out_root = get_video_settings().output_dir.resolve()
    path = Path(raw or "").resolve()
    if not raw or out_root not in path.parents or not path.is_file():
        raise HTTPException(404)
    suffix = f"-{get_format(key).slug}" if key else ""
    return FileResponse(path, media_type="video/mp4", filename=f"video-{pid}{suffix}.mp4")


def install_video(app: FastAPI) -> None:
    app.include_router(router)
    app.include_router(public_router)
    rb = app.state.rb
    rb.extras["video"] = True

    @rb.jobs.register("video.sweep")
    def _sweep(_payload):
        with rb.db.session() as db:
            Pipeline(rb, get_video_settings()).sweep(db)

    @rb.jobs.register("video.run")
    def _run(payload):
        with rb.db.session() as db:
            p = db.get(VideoProject, int(payload["project_id"]))
            if p:
                Pipeline(rb, get_video_settings()).run(db, p)

    @rb.jobs.register("video.track")
    def _track(_payload):
        with rb.db.session() as db:
            Pipeline(rb, get_video_settings()).track(db)

    rb.jobs.every("video.sweep", timedelta(hours=6))
    rb.jobs.every("video.track", timedelta(hours=6))
