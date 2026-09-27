"""Public growth endpoints and the install hook that wires growth into a RedBlue app."""

from __future__ import annotations

import json
from datetime import timedelta

from fastapi import APIRouter, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response
from markupsafe import escape

from redblue.cms import service as cms
from redblue.cms.models import Entry
from redblue.core.security import client_ip, limiter
from redblue.growth import analytics, experiments, newsletter, quality, seo
from redblue.growth.linking import related
from redblue.growth.models import Banner


def growth_router() -> APIRouter:
    r = APIRouter(include_in_schema=False)

    def rb(request: Request):
        return request.app.state.rb

    @r.get("/robots.txt")
    def robots(request: Request) -> PlainTextResponse:
        return PlainTextResponse(seo.robots_txt(rb(request).settings))

    @r.get("/sitemap.xml")
    def sitemap(request: Request) -> Response:
        with rb(request).db.session() as db:
            return Response(
                seo.sitemap_index(db, rb(request).settings), media_type="application/xml"
            )

    @r.get("/sitemaps/{collection}.xml")
    def sitemap_part(collection: str, request: Request) -> Response:
        with rb(request).db.session() as db:
            xml = seo.collection_sitemap(db, rb(request).settings, collection)
        if xml is None:
            raise HTTPException(404)
        return Response(xml, media_type="application/xml")

    @r.get("/llms.txt")
    def llms(request: Request) -> PlainTextResponse:
        with rb(request).db.session() as db:
            return PlainTextResponse(
                seo.llms_txt(db, rb(request).settings), media_type="text/markdown"
            )

    @r.get("/feed.xml")
    def feed(request: Request) -> Response:
        with rb(request).db.session() as db:
            return Response(
                seo.rss_feed(db, rb(request).settings), media_type="application/rss+xml"
            )

    @r.get("/_rb/og/{path:path}.svg")
    def og_image(path: str, request: Request) -> Response:
        settings = rb(request).settings
        title = settings.site_name
        parts = [p for p in path.split("/") if p and p != "home"]
        with rb(request).db.session() as db:
            from redblue.cms.collections import COLLECTIONS

            if len(parts) == 1:
                e = cms.live_entry(db, "page", parts[0])
            elif len(parts) == 2:
                coll = next(
                    (c for c in COLLECTIONS.values() if c.url_prefix == "/" + parts[0]), None
                )
                e = cms.live_entry(db, coll.name, parts[1]) if coll else None
            else:
                e = cms.live_entry(db, "page", "home")
            if e is not None:
                title = e.live["title"]
        return Response(
            seo.og_image_svg(title, settings.site_name),
            media_type="image/svg+xml",
            headers={
                "Cache-Control": "public, max-age=86400",
                "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'",
            },
        )

    @r.post("/_rb/beacon", status_code=204)
    async def beacon(request: Request) -> Response:
        platform = rb(request)
        if not platform.settings.analytics_enabled:
            return Response(status_code=204)
        if not limiter.hit(f"beacon:{client_ip(request)}", 120, 60):
            return Response(status_code=204)
        body = await request.body()
        if len(body) > 4096:
            return Response(status_code=204)
        try:
            payload = json.loads(body or b"{}")
        except json.JSONDecodeError:
            return Response(status_code=204)
        if not isinstance(payload, dict):
            return Response(status_code=204)
        with platform.db.session() as db:
            analytics.record(
                db,
                payload,
                ip=client_ip(request),
                user_agent=request.headers.get("user-agent", ""),
                host=request.url.hostname or "",
                dnt=request.headers.get("dnt") == "1" or request.headers.get("sec-gpc") == "1",
            )
        return Response(status_code=204)

    @r.post("/_rb/forms/{kind}")
    async def form(kind: str, request: Request):
        platform = rb(request)
        is_htmx = request.headers.get("hx-request") == "true"
        if not limiter.hit(f"form:{client_ip(request)}", 5, 300):
            return _form_reply(is_htmx, "Too many submissions, please try again later.", 429)
        data = dict(await request.form())
        referer = request.headers.get("referer", "/")
        source = "/" + referer.split("/", 3)[3] if referer.count("/") >= 3 else "/"
        try:
            with platform.db.session() as db:
                msg = newsletter.handle_form(
                    db,
                    kind,
                    {k: str(v) for k, v in data.items()},
                    secret=platform.settings.secret_key.get_secret_value(),
                    base_url=platform.settings.base_url,
                    site_name=platform.settings.site_name,
                    email_sender=platform.email,
                    source_path=source,
                    notify=platform.settings.email_from,
                )
        except newsletter.FormError as exc:
            return _form_reply(is_htmx, str(exc), 422)
        return _form_reply(is_htmx, msg, 200)

    @r.get("/newsletter/confirm/{token}")
    def confirm(token: str, request: Request):
        platform = rb(request)
        with platform.db.session() as db:
            sub = newsletter.confirm(db, token, platform.settings.secret_key.get_secret_value())
            link = None
            if sub and sub.lead_magnet:
                link = newsletter.lead_magnet_link(platform.storage, sub.lead_magnet)
        if sub is None:
            return _message(
                request,
                "Link expired",
                "This confirmation link is invalid or has expired. Please subscribe again.",
                400,
            )
        text = "Your subscription is confirmed. Thank you!"
        return _message(request, "Subscription confirmed", text, 200, download=link)

    @r.get("/newsletter/unsubscribe/{token}")
    def unsubscribe(token: str, request: Request):
        platform = rb(request)
        with platform.db.session() as db:
            ok = newsletter.unsubscribe(db, token, platform.settings.secret_key.get_secret_value())
        if not ok:
            raise HTTPException(400, "Invalid link")
        return _message(request, "Unsubscribed", "You won't receive any more emails from us.", 200)

    return r


def _form_reply(is_htmx: bool, message: str, status: int):
    if is_htmx:
        cls = "rb-form-ok" if status == 200 else "rb-error"
        role = "status" if status == 200 else "alert"
        # HTMX swaps the form; keep it on error so the visitor can fix the input.
        return HTMLResponse(
            f'<p class="{cls}" role="{role}">{escape(message)}</p>',
            status_code=200 if status == 200 else status,
            headers={} if status == 200 else {"HX-Reswap": "beforebegin"},
        )
    if status == 200:
        return RedirectResponse("/thank-you", status_code=303)
    return HTMLResponse(
        f"<p>{escape(message)}</p><p><a href='/'>Back to the site</a></p>", status_code=status
    )


def _message(request: Request, title: str, text: str, status: int, download: str | None = None):
    renderer = request.app.state.rb.extras.get("site")
    from redblue.templates.registry import CATALOG

    page = {
        "title": title,
        "url": request.url.path,
        "message": text,
        "seo": {"noindex": True},
        "blocks": [],
    }
    if download:
        page["blocks"] = [
            {
                "type": "cta",
                "heading": "Your download",
                "button_label": "Download",
                "button_url": download,
            }
        ]
    with request.app.state.rb.db.session() as db:
        return renderer.render(request, db, CATALOG["thank_you"], page, status_code=status)


# ---------------------------------------------------------------- context processors


def _growth_context(request: Request, ctx: dict) -> None:
    page = ctx["page"]
    platform = request.app.state.rb
    with platform.db.session() as db:
        if page.get("entry_id"):
            entry = db.get(Entry, page["entry_id"])
            if entry is not None:
                page.setdefault(
                    "related",
                    related(db, entry, k=3, default_locale=platform.settings.default_locale),
                )
        exps = experiments.running_for(db, page["url"])
        if exps:
            ua = request.headers.get("user-agent", "")
            visitor = analytics.visitor_hash(db, client_ip(request), ua, request.url.hostname or "")
            chosen = {e.key: experiments.assign(e, visitor) for e in exps}
            ctx["experiments"] = {k: v["key"] for k, v in chosen.items()}
            ctx["variant_text"] = lambda key, default, _c=chosen: (
                (_c[key].get("value") or default) if key in _c else default
            )
        now = __import__("redblue.core.db", fromlist=["utcnow"]).utcnow()
        banner = next(
            (
                b
                for b in db.query(Banner).filter(Banner.active.is_(True))
                if page["url"].startswith(b.path_prefix)
                and (b.starts_at is None or b.starts_at <= now)
                and (b.ends_at is None or b.ends_at >= now)
            ),
            None,
        )
        if banner:
            ctx["banner"] = {
                "id": banner.id,
                "message": banner.message,
                "cta_label": banner.cta_label,
                "cta_url": banner.cta_url,
                "frequency_days": banner.frequency_days,
            }


def install_growth(app: FastAPI) -> None:
    from redblue.templates.site import add_context_processor

    platform = app.state.rb
    settings = platform.settings
    app.include_router(growth_router())
    cms.register_publish_check(
        lambda db, entry: quality.quality_problems(
            db,
            entry,
            min_words=settings.min_publish_words,
            max_similarity=settings.max_duplicate_similarity,
        )
    )
    add_context_processor(_growth_context)

    jobs = platform.jobs

    @jobs.register("growth.weekly_report")
    def _weekly(_payload):
        from redblue.growth.agent import GrowthAgent

        with platform.db.session() as db:
            GrowthAgent(platform).weekly_report(db)

    @jobs.register("cms.publish_scheduled")
    def _publish(_payload):
        with platform.db.session() as db:
            cms.publish_due(db)

    jobs.every("growth.weekly_report", timedelta(days=7))
    jobs.every("cms.publish_scheduled", timedelta(minutes=1))
