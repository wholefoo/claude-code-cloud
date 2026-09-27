"""Admin pages for the video pipeline: ranked trends, projects, script editing, preview and
human approval. Mounted under /admin/video by :func:`install_video`."""

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import ValidationError
from sqlalchemy import select

from redblue.admin.app import DB, Writer, back, need
from redblue.admin.app import HERE as ADMIN_DIR
from redblue.core.auth import Role
from redblue.templates.env import make_templates
from redblue.video import performance
from redblue.video.config import get_video_settings
from redblue.video.models import MetricSnapshot, Publication, Trend, VideoProject
from redblue.video.pipeline import Pipeline, description
from redblue.video.schemas import Script
from redblue.video.templates import FORMATS, TEMPLATES, get_format

templates = make_templates([Path(__file__).parent / "templates", ADMIN_DIR / "templates"])
router = APIRouter(prefix="/admin/video", include_in_schema=False)


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
    )


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
