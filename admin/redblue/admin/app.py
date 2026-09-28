"""Admin UI: content, workflow, media, audience, growth, experiments, security, observability,
users. Server-rendered with Jinja2 + HTMX; every state change needs a role and a CSRF token."""

import csv
import io
import json
import secrets
from datetime import datetime
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from redblue.cms import service as cms
from redblue.cms.agent import ContentAgent
from redblue.cms.blocks import BLOCK_TYPES
from redblue.cms.collections import COLLECTIONS, get_collection
from redblue.cms.models import Entry, Media, Redirect, Status
from redblue.cms.seo import SCHEMA_TYPES, SEOFields, seo_warnings
from redblue.core import auth
from redblue.core.auth import Role, User
from redblue.core.context import get_db
from redblue.core.security import rate_limit, safe_redirect_target
from redblue.core.storage import UploadRejected, image_dimensions
from redblue.growth import analytics, experiments, freshness, linking, questions, seo, utm
from redblue.growth.agent import GrowthAgent, RepurposingAgent
from redblue.growth.models import (
    Banner,
    Experiment,
    GrowthReportRecord,
    Lead,
    SocialDraft,
    Subscriber,
)
from redblue.templates.env import make_templates
from redblue.templates.registry import CATEGORIES, by_category

HERE = Path(__file__).parent
templates = make_templates([HERE / "templates"])
DB = Annotated[Session, Depends(get_db)]


class LoginRequired(Exception):
    pass


def admin_user(request: Request, db: DB) -> User:
    user = auth.current_user_optional(request, db)
    if user is None or not user.has_role(Role.writer):
        raise LoginRequired()
    return user


Writer = Annotated[User, Depends(admin_user)]


def need(user: User, role: Role) -> None:
    if not user.has_role(role):
        raise HTTPException(403, f"Requires the {role.name} role.")


def render(request: Request, name: str, user: User | None, **ctx) -> HTMLResponse:
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
    )
    return templates.TemplateResponse(request, f"admin/{name}", ctx)


def back(path: str, msg: str = "") -> RedirectResponse:
    from urllib.parse import quote

    path, _, fragment = path.partition("#")
    sep = "&" if "?" in path else "?"
    url = path + (f"{sep}msg={quote(msg)}" if msg else "") + (f"#{fragment}" if fragment else "")
    return RedirectResponse(safe_redirect_target(url, "/admin"), status_code=303)


def _lines(text: str) -> list[str]:
    return [line.strip() for line in (text or "").splitlines() if line.strip()]


router = APIRouter(prefix="/admin", include_in_schema=False)


# ---------------------------------------------------------------- auth


@router.get("/login")
def login_form(request: Request, next: str = "/admin") -> HTMLResponse:
    providers = list(request.app.state.rb.settings.oauth_providers())
    return render(
        request,
        "login.html",
        None,
        next=safe_redirect_target(next, "/admin"),
        providers=providers,
        error="",
    )


@router.post("/login", dependencies=[Depends(rate_limit("admin-login", 10, 300))])
def login(
    request: Request,
    db: DB,
    email: Annotated[str, Form()],
    password: Annotated[str, Form()],
    next: Annotated[str, Form()] = "/admin",
):
    user = auth.authenticate(db, email, password)
    if user is None or not user.has_role(Role.writer):
        resp = render(
            request,
            "login.html",
            None,
            next=safe_redirect_target(next, "/admin"),
            providers=[],
            error="Incorrect email or password.",
        )
        resp.status_code = 401
        return resp
    resp = RedirectResponse(safe_redirect_target(next, "/admin"), status_code=303)
    auth.login(request, resp, user)
    return resp


@router.post("/logout")
def logout() -> RedirectResponse:
    resp = RedirectResponse("/admin/login", status_code=303)
    auth.logout(resp)
    return resp


@router.get("/oauth/{provider}/start")
def oauth_start(provider: str, request: Request) -> RedirectResponse:
    p = request.app.state.rb.settings.oauth_providers().get(provider)
    if p is None:
        raise HTTPException(404)
    redirect_uri = f"{request.app.state.rb.settings.base_url}/admin/oauth/{provider}/callback"
    url, state = p.authorization_redirect(redirect_uri)
    resp = RedirectResponse(url, status_code=303)
    resp.set_cookie(
        "rb_oauth_state",
        state,
        max_age=600,
        httponly=True,
        samesite="lax",
        secure=request.app.state.rb.settings.secure_cookies,
        path="/admin/oauth",
    )
    return resp


@router.get("/oauth/{provider}/callback")
def oauth_callback(provider: str, request: Request, db: DB, code: str = "", state: str = ""):
    settings = request.app.state.rb.settings
    p = settings.oauth_providers().get(provider)
    expected = request.cookies.get("rb_oauth_state", "")
    if p is None or not code or not expected or not secrets.compare_digest(expected, state):
        raise HTTPException(400, "Invalid OAuth response.")
    subject, email, name = p.fetch_identity(
        code, f"{settings.base_url}/admin/oauth/{provider}/callback"
    )
    user = auth.upsert_oauth_user(db, provider, subject, email, name)
    if not user.has_role(Role.writer):
        return back(
            "/admin/login",
            "Your account exists but has no admin access yet. Ask an admin to grant you a role.",
        )
    resp = RedirectResponse("/admin", status_code=303)
    resp.delete_cookie("rb_oauth_state", path="/admin/oauth")
    auth.login(request, resp, user)
    return resp


# ---------------------------------------------------------------- dashboard


@router.get("")
def dashboard(request: Request, db: DB, user: Writer) -> HTMLResponse:
    rb = request.app.state.rb
    counts = dict(db.execute(select(Entry.status, func.count()).group_by(Entry.status)).all())
    review = list(
        db.scalars(
            select(Entry)
            .where(Entry.status.in_([Status.in_review, Status.approved]))
            .order_by(Entry.updated_at.desc())
            .limit(10)
        )
    )
    last_report = db.scalar(
        select(GrowthReportRecord).order_by(GrowthReportRecord.created_at.desc()).limit(1)
    )
    obs = None
    try:
        from redblue.observe.dashboard import dashboard_snapshot

        obs = dashboard_snapshot(db, ai=rb.ai)
    except Exception:  # noqa: BLE001, S110 - observability is optional on the dashboard
        obs = None
    return render(
        request,
        "dashboard.html",
        user,
        counts={s.value: counts.get(s, 0) for s in Status},
        review=review,
        traffic=analytics.summary(db, days=7),
        report=last_report.report if last_report else None,
        obs=obs,
        costs=rb.ai.cost_summary(days=30),
        ai_available=rb.ai.available,
    )


# ---------------------------------------------------------------- content


@router.get("/content")
def content_list(
    request: Request, db: DB, user: Writer, collection: str = "", status: str = "", q: str = ""
) -> HTMLResponse:
    query = select(Entry).order_by(Entry.updated_at.desc()).limit(200)
    if collection:
        query = query.where(Entry.collection == collection)
    if status:
        query = query.where(Entry.status == Status(status))
    if q:
        query = query.where(func.lower(Entry.title).like(f"%{q.lower()}%"))
    return render(
        request,
        "content_list.html",
        user,
        entries=list(db.scalars(query)),
        collection=collection,
        status=status,
        q=q,
        statuses=[s.value for s in Status],
    )


@router.get("/content/new")
def content_new(request: Request, user: Writer, collection: str = "post") -> HTMLResponse:
    get_collection(collection)
    return render(
        request,
        "content_edit.html",
        user,
        entry=None,
        collection=collection,
        form=_empty_form(collection),
        errors=[],
        warnings=[],
        problems=[],
        revisions=[],
        block_types=BLOCK_TYPES,
        schema_types=SCHEMA_TYPES,
    )


def _empty_form(collection: str) -> dict:
    example = [
        {
            "type": "answer",
            "question": "What is this page about?",
            "answer": "A one to three sentence direct answer.",
        },
        {"type": "paragraph", "text": "Write here. Use **bold**, *italic* and [links](/about)."},
    ]
    return {
        "title": "",
        "slug": "",
        "summary": "",
        "blocks": json.dumps(example, indent=2),
        "data": json.dumps({"page_type": "about"} if collection == "page" else {}, indent=2),
        "tags": "",
        "category": "",
        "seo_title": "",
        "seo_description": "",
        "canonical": "",
        "schema_type": "",
        "noindex": False,
        "target_questions": "",
        "keywords": "",
        "ai_label": False,
        "locale": "en",
    }


def _form_from_entry(e: Entry) -> dict:
    seo_ = e.seo or {}
    return {
        "title": e.title,
        "slug": e.slug,
        "summary": e.summary,
        "blocks": json.dumps(e.blocks, indent=2),
        "data": json.dumps(e.data, indent=2),
        "tags": ", ".join(e.tags),
        "category": e.category or "",
        "seo_title": seo_.get("title", ""),
        "seo_description": seo_.get("description", ""),
        "canonical": seo_.get("canonical", ""),
        "schema_type": seo_.get("schema_type", ""),
        "noindex": seo_.get("noindex", False),
        "target_questions": "\n".join(seo_.get("target_questions", [])),
        "keywords": ", ".join(seo_.get("keywords", [])),
        "ai_label": e.ai_label,
        "locale": e.locale,
    }


def _parse_form(form: dict) -> cms.EntryInput:
    try:
        blocks = json.loads(form.get("blocks") or "[]")
        data = json.loads(form.get("data") or "{}")
    except json.JSONDecodeError as exc:
        raise ValueError(f"Blocks/data must be valid JSON: {exc}") from exc
    seo_ = SEOFields(
        title=form.get("seo_title") or None,
        description=form.get("seo_description") or None,
        canonical=form.get("canonical") or None,
        schema_type=form.get("schema_type") or None,
        noindex=form.get("noindex") == "on",
        target_questions=_lines(form.get("target_questions", "")),
        keywords=[k.strip() for k in form.get("keywords", "").split(",") if k.strip()],
    )
    return cms.EntryInput(
        title=form.get("title", ""),
        slug=form.get("slug") or None,
        summary=form.get("summary", ""),
        blocks=blocks,
        data=data,
        seo=seo_,
        tags=[t.strip() for t in form.get("tags", "").split(",") if t.strip()],
        category=form.get("category") or None,
        locale=form.get("locale") or "en",
        ai_label=form.get("ai_label") == "on",
    )


def _errors(exc: Exception) -> list[str]:
    if isinstance(exc, ValidationError):
        return [f"{'.'.join(str(x) for x in e['loc'])}: {e['msg']}" for e in exc.errors()][:20]
    return [str(exc)]


@router.post("/content/new")
async def content_create(request: Request, db: DB, user: Writer, collection: str = "post"):
    form = {k: str(v) for k, v in (await request.form()).items()}
    try:
        entry = cms.create_entry(db, collection, _parse_form(form), user)
    except (ValidationError, ValueError, cms.WorkflowError) as exc:
        return render(
            request,
            "content_edit.html",
            user,
            entry=None,
            collection=collection,
            form=form,
            errors=_errors(exc),
            warnings=[],
            problems=[],
            revisions=[],
            block_types=BLOCK_TYPES,
            schema_types=SCHEMA_TYPES,
        )
    return back(f"/admin/content/{entry.id}", "Draft created.")


def _entry(db: Session, entry_id: int) -> Entry:
    e = db.get(Entry, entry_id)
    if e is None:
        raise HTTPException(404)
    return e


@router.get("/content/{entry_id:int}")
def content_edit(entry_id: int, request: Request, db: DB, user: Writer) -> HTMLResponse:
    e = _entry(db, entry_id)
    return _edit_page(request, db, user, e, _form_from_entry(e), [])


def _edit_page(request, db, user, e: Entry, form: dict, errors: list[str]) -> HTMLResponse:
    warnings = seo_warnings(e.title, SEOFields(**(e.seo or {})), e.summary)
    return render(
        request,
        "content_edit.html",
        user,
        entry=e,
        collection=e.collection,
        form=form,
        errors=errors,
        warnings=warnings,
        problems=cms.publish_problems(db, e),
        revisions=cms.revisions(db, e.id),
        block_types=BLOCK_TYPES,
        schema_types=SCHEMA_TYPES,
        public_path=get_collection(e.collection).path_for(e.live_slug or e.slug, e.locale),
    )


@router.post("/content/{entry_id:int}")
async def content_save(entry_id: int, request: Request, db: DB, user: Writer):
    e = _entry(db, entry_id)
    form = {k: str(v) for k, v in (await request.form()).items()}
    try:
        cms.update_entry(db, e, _parse_form(form), user, note=form.get("note", ""))
    except cms.PermissionDenied as exc:
        raise HTTPException(403, str(exc)) from exc
    except (ValidationError, ValueError, cms.WorkflowError) as exc:
        db.rollback()
        e = _entry(db, entry_id)
        return _edit_page(request, db, user, e, form, _errors(exc))
    return back(f"/admin/content/{entry_id}", "Saved as draft.")


@router.post("/content/{entry_id:int}/workflow")
def content_workflow(
    entry_id: int,
    db: DB,
    user: Writer,
    action: Annotated[str, Form()],
    note: Annotated[str, Form()] = "",
    publish_at: Annotated[str, Form()] = "",
):
    e = _entry(db, entry_id)
    try:
        match action:
            case "submit":
                cms.submit_for_review(db, e, user)
                msg = "Submitted for review."
            case "approve":
                cms.approve(db, e, user)
                msg = "Approved."
            case "request_changes":
                cms.request_changes(db, e, user, note)
                msg = "Sent back to the writer."
            case "publish":
                at = datetime.fromisoformat(publish_at) if publish_at else None
                cms.publish(db, e, user, at=at)
                msg = "Scheduled." if e.status == Status.scheduled else "Published."
            case "unpublish":
                cms.unpublish(db, e, user)
                msg = "Unpublished."
            case _:
                raise HTTPException(400)
    except cms.PublishBlocked as exc:
        return back(f"/admin/content/{entry_id}", "Blocked: " + "; ".join(exc.problems))
    except (cms.WorkflowError, ValueError) as exc:
        return back(f"/admin/content/{entry_id}", str(exc))
    return back(f"/admin/content/{entry_id}", msg)


@router.post("/content/{entry_id:int}/rollback")
def content_rollback(entry_id: int, db: DB, user: Writer, revision: Annotated[int, Form()]):
    try:
        cms.rollback(db, _entry(db, entry_id), revision, user)
    except cms.WorkflowError as exc:
        return back(f"/admin/content/{entry_id}", str(exc))
    return back(f"/admin/content/{entry_id}", f"Restored revision {revision} as a draft.")


@router.post("/content/{entry_id:int}/refresh")
def content_refresh(
    entry_id: int, request: Request, db: DB, user: Writer, instructions: Annotated[str, Form()] = ""
):
    need(user, Role.editor)
    if not request.app.state.rb.ai.available:
        return back(f"/admin/content/{entry_id}", "Set ANTHROPIC_API_KEY to use the content agent.")
    ContentAgent(request.app.state.rb).refresh(db, _entry(db, entry_id), instructions)
    return back(f"/admin/content/{entry_id}", "The content agent proposed a refresh as a draft.")


@router.post("/content/{entry_id:int}/repurpose")
def content_repurpose(entry_id: int, request: Request, db: DB, user: Writer):
    e = _entry(db, entry_id)
    if not e.is_live:
        return back(f"/admin/content/{entry_id}", "Publish the entry before repurposing it.")
    RepurposingAgent(request.app.state.rb).repurpose(db, e)
    return back("/admin/growth/social", "Drafts created. Nothing has been posted.")


@router.get("/content/{entry_id:int}/preview")
def content_preview(entry_id: int, request: Request, db: DB, user: Writer):
    e = _entry(db, entry_id)
    renderer = request.app.state.rb.extras["site"]
    snapshot = e.snapshot()
    saved = e.live, e.live_slug
    e.live, e.live_slug = snapshot, e.slug  # render the working copy without saving it
    try:
        resp = renderer.entry_page(request, db, e, get_collection(e.collection))
    finally:
        e.live, e.live_slug = saved
        db.expire(e)
    resp.headers["X-Robots-Tag"] = "noindex"
    return resp


@router.post("/content/draft")
def content_ai_draft(
    request: Request,
    db: DB,
    user: Writer,
    collection: Annotated[str, Form()],
    brief: Annotated[str, Form()],
    questions_: Annotated[str, Form(alias="questions")] = "",
):
    e = ContentAgent(request.app.state.rb).draft(db, collection, brief, _lines(questions_))
    return back(
        f"/admin/content/{e.id}", "Draft created by the content agent. Review it before submitting."
    )


# ---------------------------------------------------------------- media & redirects


@router.get("/media")
def media_list(request: Request, db: DB, user: Writer) -> HTMLResponse:
    items = list(db.scalars(select(Media).order_by(Media.created_at.desc()).limit(200)))
    return render(request, "media.html", user, items=items)


@router.post("/media")
async def media_upload(
    request: Request,
    db: DB,
    user: Writer,
    file: Annotated[UploadFile, File()],
    alt: Annotated[str, Form()],
    caption: Annotated[str, Form()] = "",
):
    if not alt.strip():
        return back("/admin/media", "Alt text is required.")
    data = await file.read(request.app.state.rb.settings.max_upload_bytes + 1)
    try:
        stored = request.app.state.rb.storage.put(data, file.filename or "upload")
    except UploadRejected as exc:
        return back("/admin/media", str(exc))
    dims = image_dimensions(data) if stored.mime.startswith("image/") else None
    db.add(
        Media(
            key=stored.key,
            mime=stored.mime,
            size=stored.size,
            sha256=stored.sha256,
            alt=alt.strip()[:300],
            caption=caption[:500],
            uploaded_by=user.id,
            width=dims[0] if dims else None,
            height=dims[1] if dims else None,
        )
    )
    return back("/admin/media", f"Uploaded. Use media_key “{stored.key}” in image blocks.")


@router.get("/redirects")
def redirects(request: Request, db: DB, user: Writer) -> HTMLResponse:
    return render(
        request,
        "redirects.html",
        user,
        items=list(db.scalars(select(Redirect).order_by(Redirect.created_at.desc()))),
    )


@router.post("/redirects")
def redirect_add(
    db: DB, user: Writer, from_path: Annotated[str, Form()], to_path: Annotated[str, Form()]
):
    need(user, Role.editor)
    try:
        cms.add_redirect(db, from_path.strip(), to_path.strip())
    except cms.WorkflowError as exc:
        return back("/admin/redirects", str(exc))
    return back("/admin/redirects", "Redirect saved.")


@router.post("/redirects/{rid}/delete")
def redirect_delete(rid: int, db: DB, user: Writer):
    need(user, Role.editor)
    r = db.get(Redirect, rid)
    if r:
        db.delete(r)
    return back("/admin/redirects", "Redirect removed.")


# ---------------------------------------------------------------- audience


@router.get("/audience")
def audience(request: Request, db: DB, user: Writer) -> HTMLResponse:
    need(user, Role.editor)
    subs = list(db.scalars(select(Subscriber).order_by(Subscriber.consent_at.desc()).limit(500)))
    leads = list(db.scalars(select(Lead).order_by(Lead.created_at.desc()).limit(200)))
    return render(request, "audience.html", user, subs=subs, leads=leads)


@router.get("/audience/subscribers.csv")
def subscribers_csv(db: DB, user: Writer) -> Response:
    need(user, Role.admin)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["email", "list", "confirmed_at", "consent_text", "consent_at", "source"])
    for s in db.scalars(select(Subscriber).where(Subscriber.status == "confirmed")):
        w.writerow(
            [s.email, s.list_name, s.confirmed_at, s.consent_text, s.consent_at, s.consent_source]
        )
    return Response(
        buf.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=subscribers.csv"},
    )


# ---------------------------------------------------------------- growth


@router.get("/growth")
def growth(request: Request, db: DB, user: Writer) -> HTMLResponse:
    last = db.scalar(
        select(GrowthReportRecord).order_by(GrowthReportRecord.created_at.desc()).limit(1)
    )
    settings = request.app.state.rb.settings
    crawlers = [
        {
            "name": n,
            "purpose": p,
            "allowed": settings.ai_crawler_overrides.get(n, settings.ai_crawlers_allowed),
        }
        for n, p in seo.AI_CRAWLERS.items()
    ]
    return render(
        request,
        "growth.html",
        user,
        report=last.report if last else None,
        traffic=analytics.summary(db, days=30),
        conversions=analytics.goal_conversions(db, days=30),
        stale=freshness.stale_pages(db),
        coverage=questions.coverage(db),
        links=linking.link_suggestions(db, limit=20),
        crawlers=crawlers,
        calendar=utm.content_calendar(db),
    )


@router.post("/growth/report")
def growth_report(request: Request, db: DB, user: Writer):
    GrowthAgent(request.app.state.rb).weekly_report(db)
    return back("/admin/growth", "Growth report generated.")


@router.post("/growth/audit")
def growth_audit(request: Request, db: DB, user: Writer) -> HTMLResponse:
    from fastapi.testclient import TestClient

    from redblue.cms.service import live_entries
    from redblue.growth.audit import audit_site

    settings = request.app.state.rb.settings
    paths = ["/"] + [
        get_collection(e.collection).path_for(e.live_slug, e.locale, settings.default_locale)
        for e in live_entries(db, None, limit=300)
    ]
    with TestClient(request.app, base_url=settings.base_url) as client:
        report = audit_site(client, list(dict.fromkeys(paths)))
    return render(request, "audit.html", user, report=report)


@router.post("/growth/utm")
def growth_utm(
    request: Request,
    user: Writer,
    url: Annotated[str, Form()],
    source: Annotated[str, Form()],
    medium: Annotated[str, Form()],
    campaign: Annotated[str, Form()],
) -> HTMLResponse:
    try:
        link = utm.build_utm_url(url, source=source, medium=medium, campaign=campaign)
        return HTMLResponse(f'<p class="rb-form-ok"><code>{_escape_html(link)}</code></p>')
    except ValueError as exc:
        return HTMLResponse(f'<p class="rb-error" role="alert">{_escape_html(str(exc))}</p>')


def _escape_html(s: str) -> str:
    from markupsafe import escape

    return str(escape(s))


@router.post("/growth/search-console")
async def growth_search_console(db: DB, user: Writer, file: Annotated[UploadFile, File()]):
    need(user, Role.editor)
    raw = (await file.read(5 * 1024 * 1024)).decode("utf-8", errors="replace")
    n = questions.import_search_console_csv(db, raw)
    return back("/admin/growth", f"Imported {n} Search Console rows.")


@router.get("/growth/social")
def social(request: Request, db: DB, user: Writer) -> HTMLResponse:
    drafts = list(
        db.scalars(
            select(SocialDraft)
            .where(SocialDraft.status != "archived")
            .order_by(SocialDraft.created_at.desc())
            .limit(100)
        )
    )
    return render(request, "social.html", user, drafts=drafts)


@router.post("/growth/social/{did}")
def social_update(
    did: int,
    db: DB,
    user: Writer,
    status: Annotated[str, Form()],
    text: Annotated[str, Form()] = "",
):
    d = db.get(SocialDraft, did)
    if d is None or status not in ("draft", "approved", "archived"):
        raise HTTPException(404)
    d.status = status
    if text:
        d.text = text[:5000]
    return back("/admin/growth/social", "Saved. Copy approved drafts to post them yourself.")


# ---------------------------------------------------------------- experiments & banners


@router.get("/experiments")
def experiments_page(request: Request, db: DB, user: Writer) -> HTMLResponse:
    exps = list(db.scalars(select(Experiment).order_by(Experiment.created_at.desc())))
    return render(
        request,
        "experiments.html",
        user,
        items=[(e, experiments.results(db, e)) for e in exps],
        banners=list(db.scalars(select(Banner))),
    )


@router.post("/experiments")
def experiment_create(
    db: DB,
    user: Writer,
    key: Annotated[str, Form()],
    name: Annotated[str, Form()],
    goal: Annotated[str, Form()],
    variants: Annotated[str, Form()],
    path: Annotated[str, Form()] = "",
):
    need(user, Role.editor)
    vs = [{"key": f"v{i}", "value": v, "weight": 1} for i, v in enumerate(_lines(variants))]
    if len(vs) < 2:
        return back("/admin/experiments", "Add at least two variants (one per line).")
    if db.scalar(select(Experiment).where(Experiment.key == key)):
        return back("/admin/experiments", "Experiment key already exists.")
    db.add(
        Experiment(
            key=key.strip()[:80],
            name=name[:200],
            goal=goal.strip()[:100],
            path=path.strip() or None,
            variants=vs,
        )
    )
    return back("/admin/experiments", "Experiment created (not running yet).")


@router.post("/experiments/{eid}/status")
def experiment_status(eid: int, db: DB, user: Writer, status: Annotated[str, Form()]):
    need(user, Role.publisher)
    e = db.get(Experiment, eid)
    if e is None or status not in ("running", "stopped", "draft"):
        raise HTTPException(404)
    e.status = status
    return back("/admin/experiments", f"Experiment {status}.")


@router.post("/banners")
def banner_create(
    db: DB,
    user: Writer,
    message: Annotated[str, Form()],
    cta_label: Annotated[str, Form()] = "",
    cta_url: Annotated[str, Form()] = "",
    path_prefix: Annotated[str, Form()] = "/",
    frequency_days: Annotated[int, Form()] = 7,
):
    need(user, Role.publisher)
    from redblue.core.security import is_safe_link

    if cta_url and not is_safe_link(cta_url):
        return back("/admin/experiments", "Unsafe CTA link.")
    db.add(
        Banner(
            message=message[:300],
            cta_label=cta_label[:60],
            cta_url=cta_url[:500],
            path_prefix=path_prefix or "/",
            frequency_days=max(1, frequency_days),
            active=True,
        )
    )
    return back("/admin/experiments", "Banner is live.")


@router.post("/banners/{bid}/toggle")
def banner_toggle(bid: int, db: DB, user: Writer):
    need(user, Role.publisher)
    b = db.get(Banner, bid)
    if b:
        b.active = not b.active
    return back("/admin/experiments", "Banner updated.")


# ---------------------------------------------------------------- security, observe, catalog


@router.get("/security")
def security(request: Request, user: Writer) -> HTMLResponse:
    path = Path(".redblue/last-report.json")
    data = json.loads(path.read_text()) if path.exists() else None
    return render(request, "security.html", user, data=data)


@router.post("/security/scan")
def security_scan(request: Request, user: Writer):
    need(user, Role.editor)
    request.app.state.rb.jobs.enqueue("gate.scan", {})
    return back(
        "/admin/security",
        "Scan queued. Run `redblue jobs` (or the worker) to process it; results appear here.",
    )


@router.get("/observe")
def observe(request: Request, db: DB, user: Writer) -> HTMLResponse:
    need(user, Role.editor)
    from redblue.observe.dashboard import anomalies_view, dashboard_snapshot

    return render(
        request,
        "observe.html",
        user,
        snap=dashboard_snapshot(db, ai=request.app.state.rb.ai),
        anomalies=anomalies_view(db),
    )


@router.get("/templates")
def catalog(request: Request, user: Writer) -> HTMLResponse:
    return render(request, "catalog.html", user, groups=by_category(), categories=CATEGORIES)


# ---------------------------------------------------------------- users


@router.get("/users")
def users(request: Request, db: DB, user: Writer) -> HTMLResponse:
    need(user, Role.admin)
    return render(
        request,
        "users.html",
        user,
        items=list(db.scalars(select(User))),
        roles=[r.name for r in Role],
        new_password="",
        new_key="",
    )


@router.post("/users")
def user_create(
    request: Request,
    db: DB,
    user: Writer,
    email: Annotated[str, Form()],
    role: Annotated[str, Form()],
    name: Annotated[str, Form()] = "",
):
    need(user, Role.admin)
    password = secrets.token_urlsafe(12)
    try:
        auth.create_user(db, email, password, Role[role], name)
    except (ValueError, KeyError) as exc:
        return back("/admin/users", str(exc))
    return render(
        request,
        "users.html",
        user,
        items=list(db.scalars(select(User))),
        roles=[r.name for r in Role],
        new_password=password,
        new_key="",
    )


@router.post("/users/{uid}")
def user_update(
    uid: int,
    db: DB,
    user: Writer,
    role: Annotated[str, Form()],
    active: Annotated[str, Form()] = "",
    bio: Annotated[str, Form()] = "",
    credentials: Annotated[str, Form()] = "",
):
    need(user, Role.admin)
    target = db.get(User, uid)
    if target is None:
        raise HTTPException(404)
    if target.id == user.id and (Role[role] < Role.admin or active != "on"):
        return back("/admin/users", "You can't remove your own admin access.")
    target.role = Role[role]
    was_active = target.is_active
    target.is_active = active == "on"
    target.bio, target.credentials = bio[:2000], credentials[:500]
    if was_active and not target.is_active:
        target.session_version += 1
    return back("/admin/users", "User updated.")


@router.post("/users/{uid}/api-key")
def user_api_key(request: Request, uid: int, db: DB, user: Writer, name: Annotated[str, Form()]):
    need(user, Role.admin)
    target = db.get(User, uid)
    if target is None:
        raise HTTPException(404)
    key = auth.create_api_key(db, target, name[:100] or "api")
    return render(
        request,
        "users.html",
        user,
        items=list(db.scalars(select(User))),
        roles=[r.name for r in Role],
        new_password="",
        new_key=key,
    )


def install_admin(app: FastAPI) -> None:
    app.mount("/static/admin", StaticFiles(directory=str(HERE / "static")), name="admin-static")
    app.include_router(router)
    try:
        from redblue.observe.dashboard import observe_router

        app.include_router(observe_router(), prefix="/admin/observe/api")
    except ImportError:
        pass

    @app.exception_handler(LoginRequired)
    async def _login_required(request: Request, exc: LoginRequired):
        target = safe_redirect_target(request.url.path, "/admin")
        return RedirectResponse(f"/admin/login?next={target}", status_code=303)

    jobs = app.state.rb.jobs

    @jobs.register("gate.scan")
    def _gate_scan(_payload):
        try:
            from redblue.gate.config import load_config
            from redblue.gate.gate import run_gate
            from redblue.gate.report import as_json
        except ImportError:
            return
        report, decision = run_gate(load_config(None, "."), ".", use_ai=True)
        out = Path(".redblue")
        out.mkdir(exist_ok=True)
        (out / "last-report.json").write_text(as_json(report, decision))
