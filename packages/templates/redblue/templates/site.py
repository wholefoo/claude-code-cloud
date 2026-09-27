"""The public site: renders live CMS entries with the template library.

Routes: ``/``, ``/{slug}`` (pages), ``/{prefix}`` listings, ``/{prefix}/{slug}`` entries,
tag/category archives, author profiles, search, localized ``/{locale}/...`` variants, media,
redirects, a styled 404 and maintenance mode.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from fastapi import APIRouter, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from markupsafe import Markup
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.exceptions import HTTPException as StarletteHTTPException

from redblue.cms import service
from redblue.cms.collections import COLLECTIONS, CollectionDef, get_collection
from redblue.cms.models import Entry, Media
from redblue.cms.render import first_answer, toc
from redblue.core.auth import User
from redblue.core.context import Platform
from redblue.templates.env import STATIC_DIR, make_templates
from redblue.templates.jsonld import page_graph
from redblue.templates.registry import CATALOG, PageType, listing_type_for, page_type_for_collection
from redblue.templates.tokens import Tokens

ContextProcessor = Callable[[Request, dict], None]
context_processors: list[ContextProcessor] = []
PAGE_SIZE = 20


def add_context_processor(fn: ContextProcessor) -> ContextProcessor:
    """Growth, observability and sites use this to add data before rendering."""
    if fn not in context_processors:
        context_processors.append(fn)
    return fn


@dataclass
class BlockContext:
    """Passed into the block macros: resolves media and renders platform forms."""

    request: Request
    db: Session
    templates: Any
    csrf_token: str

    def media(self, block: dict) -> dict:
        if block.get("media_key"):
            m = self.db.scalar(select(Media).where(Media.key == block["media_key"]))
            if m:
                return {"src": f"/media/{m.key}", "width": m.width, "height": m.height}
        return {
            "src": block.get("src", ""),
            "width": block.get("width"),
            "height": block.get("height"),
        }

    def form(self, block: dict) -> Markup:
        tpl = self.templates.env.get_template("partials/form.html")
        return Markup(tpl.render(form=block, csrf_token=self.csrf_token))  # noqa: S704  # nosec B704


def _site(p: Platform, db: Session, locale: str) -> dict:
    s = p.settings
    nav: list[dict] = []
    for page in db.scalars(
        select(Entry)
        .where(Entry.collection == "page", Entry.live.is_not(None), Entry.locale == locale)
        .order_by(Entry.sort_order)
    ):
        if "nav" in (page.live or {}).get("tags", []):
            nav.append(
                {
                    "label": page.live["title"],
                    "path": get_collection("page").path_for(
                        page.live_slug, locale, s.default_locale
                    ),
                }
            )
    for c in COLLECTIONS.values():
        path = c.listing_path(locale, s.default_locale)
        if (
            path
            and c.name in ("post", "doc", "product", "guide")
            and db.scalar(
                select(Entry.id).where(Entry.collection == c.name, Entry.live.is_not(None)).limit(1)
            )
        ):
            nav.append({"label": c.label if c.name != "post" else "Blog", "path": path})
    footer = [
        {"label": e.live["title"], "path": f"/{e.live_slug}"}
        for e in db.scalars(
            select(Entry).where(
                Entry.collection == "page",
                Entry.live.is_not(None),
                Entry.live_slug.in_(("privacy", "terms", "cookies", "about", "contact")),
            )
        )
    ]
    return {
        "site_name": s.site_name,
        "base_url": s.base_url,
        "organization_name": s.organization_name,
        "organization_logo": s.organization_logo,
        "locales": s.locales,
        "default_locale": s.default_locale,
        "nav": nav,
        "footer_links": footer,
        "year": __import__("datetime").date.today().year,
    }


def _author(db: Session, author_id: int | None) -> dict | None:
    if not author_id:
        return None
    u = db.get(User, author_id)
    if u is None:
        return None
    return {"id": u.id, "name": u.name, "bio": u.bio, "credentials": u.credentials}


class SiteRenderer:
    def __init__(self, app: FastAPI, templates):
        self.app = app
        self.templates = templates

    @property
    def platform(self) -> Platform:
        return self.app.state.rb

    def render(
        self,
        request: Request,
        db: Session,
        page_type: PageType,
        page: dict,
        status_code: int = 200,
        locale: str | None = None,
    ) -> HTMLResponse:
        p = self.platform
        locale = locale or p.settings.default_locale
        site = _site(p, db, locale)
        seo = page.get("seo") or {}
        path = page["url"]
        title = seo.get("title") or page["title"]
        full_title = title if page_type.key == "home" else f"{title} · {p.settings.site_name}"
        description = seo.get("description") or page.get("description") or ""
        canonical = seo.get("canonical") or p.settings.base_url + path
        if canonical.startswith("/"):
            canonical = p.settings.base_url + canonical
        page.setdefault("schema_type", seo.get("schema_type") or page_type.schema_type)
        page.setdefault("locale", locale)
        csrf = getattr(request.state, "csrf_token", "")
        ctx: dict[str, Any] = {
            "request": request,
            "site": site,
            "page": page,
            "page_type": page_type,
            "meta": {
                "title": full_title,
                "description": description,
                "canonical": canonical,
                "noindex": bool(seo.get("noindex")) or status_code >= 400,
                "og_type": "article" if page_type.template.endswith("article.html") else "website",
                "og_image": page.get("og_image") or f"/_rb/og{path.rstrip('/') or '/home'}.svg",
                "alternates": page.get("alternates", []),
            },
            "csrf_token": csrf,
            "nonce": getattr(request.state, "csp_nonce", ""),
            "ctx": BlockContext(request, db, self.templates, csrf),
            "settings": p.settings,
        }
        for proc in context_processors:
            proc(request, ctx)
        ctx["jsonld"] = page_graph(site, {**page, "description": description})
        return self.templates.TemplateResponse(
            request, page_type.template, ctx, status_code=status_code
        )

    # -------------------------------------------------------------- entries

    def entry_page(
        self, request: Request, db: Session, entry: Entry, coll: CollectionDef
    ) -> HTMLResponse:
        live = entry.live or {}
        settings = self.platform.settings
        override = live.get("data", {}).get("page_type") if coll.name == "page" else None
        if coll.name == "page" and entry.live_slug == "home" and not override:
            override = "home"
        pt = page_type_for_collection(coll.name, override)
        path = coll.path_for(entry.live_slug, entry.locale, settings.default_locale)
        crumbs = [("Home", "/")]
        listing = coll.listing_path(entry.locale, settings.default_locale)
        if listing:
            crumbs.append((coll.label if coll.name != "post" else "Blog", listing))
        if path != "/":
            crumbs.append((live.get("title", ""), path))
        blocks = live.get("blocks", [])
        page = {
            "entry_id": entry.id,
            "collection": coll.name,
            "title": live.get("title", ""),
            "description": live.get("summary", ""),
            "summary": live.get("summary", ""),
            "url": path,
            "blocks": blocks,
            "data": live.get("data", {}),
            "seo": live.get("seo", {}),
            "tags": live.get("tags", []),
            "category": live.get("category"),
            "author": _author(db, live.get("author_id")),
            "published": entry.published_at.date().isoformat() if entry.published_at else None,
            "modified": (
                entry.content_updated_at.date().isoformat() if entry.content_updated_at else None
            ),
            "published_at": entry.published_at,
            "modified_at": entry.content_updated_at,
            "ai_label": live.get("ai_label", False),
            "crumbs": crumbs,
            "toc": toc(blocks),
            "answer": first_answer(blocks),
            "alternates": self._alternates(db, entry, coll),
        }
        if pt.key == "documentation":
            page["sidebar"] = [
                {
                    "title": d.live["title"],
                    "url": coll.path_for(d.live_slug, d.locale, settings.default_locale),
                }
                for d in service.live_entries(db, "doc", locale=entry.locale)
            ]
        return self.render(request, db, pt, page, locale=entry.locale)

    def _alternates(self, db: Session, entry: Entry, coll: CollectionDef) -> list[dict]:
        root = entry.translation_of or entry.id
        group = list(
            db.scalars(
                select(Entry).where(
                    Entry.live.is_not(None), (Entry.id == root) | (Entry.translation_of == root)
                )
            )
        )
        if len(group) < 2:
            return []
        base, default = self.platform.settings.base_url, self.platform.settings.default_locale
        alts = [
            {"hreflang": e.locale, "href": base + coll.path_for(e.live_slug, e.locale, default)}
            for e in group
        ]
        alts += [
            {
                "hreflang": "x-default",
                "href": next(
                    (a["href"] for a in alts if a["hreflang"] == default), alts[0]["href"]
                ),
            }
        ]
        return alts

    def listing_page(
        self,
        request: Request,
        db: Session,
        coll: CollectionDef,
        locale: str,
        *,
        tag: str | None = None,
        category: str | None = None,
        page_num: int = 1,
    ) -> HTMLResponse:
        pt = listing_type_for(coll.name) or CATALOG["archive"]
        if tag or category:
            pt = CATALOG["archive"]
        settings = self.platform.settings
        items = service.live_entries(
            db,
            coll.name,
            locale=locale,
            tag=tag,
            category=category,
            limit=PAGE_SIZE + 1,
            offset=(page_num - 1) * PAGE_SIZE,
        )
        if page_num > 1 and not items:
            raise HTTPException(404)
        base_path = coll.listing_path(locale, settings.default_locale) or f"/{coll.name}"
        label = coll.label if coll.name != "post" else "Blog"
        title = f"{label}: {tag or category}" if (tag or category) else label
        path = base_path + (f"/tag/{tag}" if tag else f"/category/{category}" if category else "")
        page = {
            "title": title,
            "description": f"{title} from {settings.site_name}.",
            "url": path,
            "items": [self._card(e, coll, settings.default_locale) for e in items[:PAGE_SIZE]],
            "page_num": page_num,
            "has_next": len(items) > PAGE_SIZE,
            "collection": coll.name,
            "crumbs": [("Home", "/"), (label, base_path)]
            + ([(title, path)] if path != base_path else []),
            "blocks": [],
            "letters": sorted({e.live["title"][:1].upper() for e in items}),
        }
        if pt.key == "faq":
            page["blocks"] = (
                [
                    {
                        "type": "faq",
                        "items": [
                            {
                                "question": e.live["title"],
                                "answer": (first_answer(e.live.get("blocks", [])) or {}).get(
                                    "answer", e.live.get("summary", "")
                                ),
                            }
                            for e in items[:PAGE_SIZE]
                        ],
                    }
                ]
                if items
                else []
            )
        return self.render(request, db, pt, page, locale=locale)

    @staticmethod
    def _card(e: Entry, coll: CollectionDef, default_locale: str) -> dict:
        live = e.live or {}
        return {
            "title": live.get("title", ""),
            "summary": live.get("summary", ""),
            "url": coll.path_for(e.live_slug, e.locale, default_locale),
            "date": e.published_at,
            "tags": live.get("tags", []),
            "data": live.get("data", {}),
        }


def install_site(app: FastAPI, *, extra_template_dirs=None) -> SiteRenderer:
    templates = make_templates(extra_template_dirs)
    renderer = SiteRenderer(app, templates)
    app.state.rb.extras["templates"] = templates
    app.state.rb.extras["site"] = renderer
    app.mount("/static/rb", StaticFiles(directory=str(STATIC_DIR)), name="rb-static")
    app.include_router(site_router(renderer))

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException):
        if exc.status_code == 404 and _wants_html(request):
            with app.state.rb.db.session() as db:
                r = service.resolve_redirect(db, request.url.path)
                if r:
                    return RedirectResponse(r.to_path, status_code=r.status_code)
                return renderer.render(
                    request,
                    db,
                    CATALOG["not_found"],
                    {
                        "title": "Page not found",
                        "url": request.url.path,
                        "description": "We couldn't find that page.",
                        "message": "The page you were looking for doesn't exist or has moved.",
                    },
                    status_code=404,
                )
        from fastapi.exception_handlers import http_exception_handler

        return await http_exception_handler(request, exc)

    @app.middleware("http")
    async def maintenance(request: Request, call_next):
        if app.state.rb.settings.maintenance_mode and not request.url.path.startswith(
            ("/admin", "/_rb", "/static")
        ):
            with app.state.rb.db.session() as db:
                resp = renderer.render(
                    request,
                    db,
                    CATALOG["maintenance"],
                    {
                        "title": "Down for maintenance",
                        "url": request.url.path,
                        "message": "We're making improvements and will be back shortly.",
                    },
                    status_code=503,
                )
            resp.headers["Retry-After"] = "600"
            return resp
        return await call_next(request)

    return renderer


def _wants_html(request: Request) -> bool:
    if request.url.path.startswith(("/api/", "/_rb/")):
        return False
    accept = request.headers.get("accept", "")
    return not accept or "text/html" in accept or "*/*" in accept


def favicon_svg(site_name: str, background: str, foreground: str) -> str:
    """A monogram favicon. Only a single A-Z/0-9 character and validated hex colours
    (``Palette`` rejects anything else) are ever placed in the markup."""
    letter = next((c for c in site_name.upper() if c.isascii() and c.isalnum()), "R")
    for colour in (background, foreground):
        if not re.fullmatch(r"#[0-9a-fA-F]{3,8}", colour):
            raise ValueError("Favicon colours must be hex values.")
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">'
        + '<rect width="64" height="64" rx="14" fill="'
        + background
        + '"/>'
        + '<text x="32" y="44" font-family="system-ui,sans-serif" font-size="36" '
        + 'font-weight="700" text-anchor="middle" fill="'
        + foreground
        + '">'
        + letter
        + "</text></svg>"
    )


def site_router(r: SiteRenderer) -> APIRouter:
    router = APIRouter(include_in_schema=False)

    def db_session(request: Request):
        return request.app.state.rb.db.session()

    @router.get("/_rb/tokens.css")
    def tokens_css(request: Request) -> Response:
        tokens = Tokens(**request.app.state.rb.settings.theme_tokens)
        return Response(
            tokens.css(), media_type="text/css", headers={"Cache-Control": "public, max-age=300"}
        )

    @router.get("/_rb/favicon.svg")
    @router.get("/favicon.ico")
    def favicon(request: Request) -> Response:
        s = request.app.state.rb.settings
        tokens = Tokens(**s.theme_tokens)
        svg = favicon_svg(s.site_name, tokens.light.primary, tokens.light.primary_text)
        return Response(
            svg, media_type="image/svg+xml", headers={"Cache-Control": "public, max-age=86400"}
        )

    @router.get("/media/{key}")
    def media(key: str, request: Request) -> Response:
        storage = request.app.state.rb.storage
        with db_session(request) as db:
            m = db.scalar(select(Media).where(Media.key == key))
        if m is None:
            raise HTTPException(404)
        try:
            data = storage.get(key)
        except (FileNotFoundError, ValueError):
            raise HTTPException(404) from None
        return Response(
            data,
            media_type=m.mime,
            headers={
                "Cache-Control": "public, max-age=31536000, immutable",
                "Content-Disposition": "inline" if m.mime.startswith("image/") else "attachment",
            },
        )

    @router.get("/search")
    def search(request: Request, q: str = "") -> HTMLResponse:
        with db_session(request) as db:
            results = service.search(db, q) if len(q.strip()) >= 2 else []
            default = request.app.state.rb.settings.default_locale
            items = [SiteRenderer._card(e, get_collection(e.collection), default) for e in results]
            return r.render(
                request,
                db,
                CATALOG["search_results"],
                {
                    "title": f"Search results for “{q}”" if q else "Search",
                    "url": "/search",
                    "query": q,
                    "items": items,
                    "seo": {"noindex": True},
                    "description": "Search this site.",
                },
            )

    @router.get("/authors/{author_id}")
    def author(author_id: int, request: Request) -> HTMLResponse:
        with db_session(request) as db:
            a = _author(db, author_id)
            if a is None:
                raise HTTPException(404)
            posts = [
                e
                for e in service.live_entries(db, None, limit=200)
                if (e.live or {}).get("author_id") == author_id
            ]
            default = request.app.state.rb.settings.default_locale
            return r.render(
                request,
                db,
                CATALOG["author"],
                {
                    "title": a["name"],
                    "url": f"/authors/{author_id}",
                    "author": a,
                    "description": a["bio"][:160] or f"Articles by {a['name']}.",
                    "items": [
                        SiteRenderer._card(e, get_collection(e.collection), default) for e in posts
                    ],
                    "schema_type": "ProfilePage",
                    "crumbs": [("Home", "/"), (a["name"], f"/authors/{author_id}")],
                },
            )

    @router.get("/")
    def home(request: Request) -> HTMLResponse:
        return _dispatch(request, [])

    @router.get("/{path:path}")
    def any_path(path: str, request: Request) -> HTMLResponse:
        return _dispatch(request, [p for p in path.split("/") if p])

    def _dispatch(request: Request, parts: list[str]) -> HTMLResponse:
        settings = request.app.state.rb.settings
        locale = settings.default_locale
        if parts and parts[0] in settings.locales and parts[0] != settings.default_locale:
            locale, parts = parts[0], parts[1:]
        with db_session(request) as db:
            if not parts:
                e = service.live_entry(db, "page", "home", locale)
                if e is None:
                    return r.render(request, db, CATALOG["home"], _welcome(settings), locale=locale)
                return r.entry_page(request, db, e, get_collection("page"))
            prefix = "/" + parts[0]
            coll = next((c for c in COLLECTIONS.values() if c.url_prefix == prefix), None)
            if coll is None:
                if len(parts) == 1:
                    e = service.live_entry(db, "page", parts[0], locale)
                    if e is not None:
                        return r.entry_page(request, db, e, get_collection("page"))
                raise HTTPException(404)
            page_num = _page_param(request)
            if len(parts) == 1 and coll.listing_page_type:
                return r.listing_page(request, db, coll, locale, page_num=page_num)
            if len(parts) == 3 and parts[1] in ("tag", "category"):
                kw = {parts[1]: parts[2]}
                return r.listing_page(request, db, coll, locale, page_num=page_num, **kw)
            if len(parts) == 2:
                e = service.live_entry(db, coll.name, parts[1], locale)
                if e is not None:
                    return r.entry_page(request, db, e, coll)
            raise HTTPException(404)

    return router


def _page_param(request: Request) -> int:
    try:
        return max(1, min(1000, int(request.query_params.get("page", "1"))))
    except ValueError:
        return 1


def _welcome(settings) -> dict:
    return {
        "title": settings.site_name,
        "url": "/",
        "description": "A new site built with RedBlue.",
        "summary": "Your site is running. Sign in to the admin to create your home page.",
        "blocks": [
            {
                "type": "answer",
                "question": "What is this site?",
                "answer": "A fresh RedBlue site. Create a page with the slug “home” in the "
                "admin to replace this welcome screen.",
            },
            {
                "type": "cta",
                "heading": "Get started",
                "text": "Open the admin to add content.",
                "button_label": "Open admin",
                "button_url": "/admin",
            },
        ],
        "crumbs": [],
    }
