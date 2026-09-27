"""Technical SEO and AEO/GEO outputs: sitemaps (split by collection, with hreflang),
robots.txt with per-crawler AI controls, llms.txt, RSS, and generated social share images."""

from __future__ import annotations

import html
import textwrap
from xml.sax.saxutils import escape as xml_escape

from sqlalchemy import select
from sqlalchemy.orm import Session

from redblue.cms.collections import COLLECTIONS, get_collection
from redblue.cms.models import Entry
from redblue.cms.service import live_entries
from redblue.core.config import Settings

# Known AI crawlers. The site owner decides per crawler (Settings.ai_crawler_overrides).
AI_CRAWLERS: dict[str, str] = {
    "GPTBot": "OpenAI training",
    "OAI-SearchBot": "OpenAI search",
    "ChatGPT-User": "ChatGPT user-initiated browsing",
    "ClaudeBot": "Anthropic training",
    "Claude-SearchBot": "Anthropic search",
    "Claude-User": "Claude user-initiated fetch",
    "PerplexityBot": "Perplexity search",
    "Perplexity-User": "Perplexity user fetch",
    "Google-Extended": "Google AI training (Gemini)",
    "Applebot-Extended": "Apple AI training",
    "CCBot": "Common Crawl",
    "Bytespider": "ByteDance",
    "Amazonbot": "Amazon",
    "meta-externalagent": "Meta AI",
    "cohere-ai": "Cohere",
    "DuckAssistBot": "DuckDuckGo AI",
    "MistralAI-User": "Mistral user fetch",
}


def _is_indexable(e: Entry) -> bool:
    return not (e.live or {}).get("seo", {}).get("noindex")


def _entry_url(settings: Settings, e: Entry) -> str:
    return settings.base_url + get_collection(e.collection).path_for(
        e.live_slug, e.locale, settings.default_locale
    )


def sitemap_index(db: Session, settings: Settings) -> str:
    names = sorted(
        {
            c
            for (c,) in db.execute(
                select(Entry.collection).where(Entry.live.is_not(None)).distinct()
            )
        }
    )
    items = "".join(
        f"<sitemap><loc>{xml_escape(settings.base_url)}/sitemaps/{n}.xml</loc></sitemap>"
        for n in names
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{items}'
        "</sitemapindex>"
    )


def collection_sitemap(db: Session, settings: Settings, collection: str) -> str | None:
    if collection not in COLLECTIONS:
        return None
    coll = get_collection(collection)
    entries = [e for e in live_entries(db, collection, limit=50_000) if _is_indexable(e)]
    groups: dict[int, list[Entry]] = {}
    for e in entries:
        groups.setdefault(e.translation_of or e.id, []).append(e)
    urls = []
    listing = coll.listing_path(settings.default_locale, settings.default_locale)
    if listing and entries:
        urls.append(f"<url><loc>{xml_escape(settings.base_url + listing)}</loc></url>")
    for e in entries:
        alts = ""
        group = groups.get(e.translation_of or e.id, [])
        if len(group) > 1:
            alts = "".join(
                f'<xhtml:link rel="alternate" hreflang="{g.locale}" '
                f'href="{xml_escape(_entry_url(settings, g))}"/>'
                for g in group
            )
        lastmod = e.content_updated_at or e.published_at
        urls.append(
            f"<url><loc>{xml_escape(_entry_url(settings, e))}</loc>"
            + (f"<lastmod>{lastmod.date().isoformat()}</lastmod>" if lastmod else "")
            + f"<changefreq>{coll.changefreq}</changefreq><priority>{coll.priority}</priority>"
            + alts
            + "</url>"
        )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9" '
        'xmlns:xhtml="http://www.w3.org/1999/xhtml">' + "".join(urls) + "</urlset>"
    )


def robots_txt(settings: Settings) -> str:
    lines = [
        "User-agent: *",
        "Disallow: /admin",
        "Disallow: /_rb/",
        "Disallow: /api/",
        "Disallow: /search",
        "Allow: /",
        "",
    ]
    for bot in AI_CRAWLERS:
        allowed = settings.ai_crawler_overrides.get(bot, settings.ai_crawlers_allowed)
        lines += [f"User-agent: {bot}", "Allow: /" if allowed else "Disallow: /", ""]
    lines.append(f"Sitemap: {settings.base_url}/sitemap.xml")
    return "\n".join(lines) + "\n"


def llms_txt(db: Session, settings: Settings, per_collection: int = 25) -> str:
    """https://llmstxt.org: a concise, curated map of the site's key pages for AI assistants."""
    out = [f"# {settings.site_name}", ""]
    home = next(iter(live_entries(db, "page", limit=200)), None)
    homepage = next((e for e in live_entries(db, "page", limit=200) if e.live_slug == "home"), home)
    if homepage and (homepage.live or {}).get("summary"):
        out += [f"> {homepage.live['summary'].strip()}", ""]
    for coll in COLLECTIONS.values():
        if not coll.in_llms_txt:
            continue
        items = [
            e
            for e in live_entries(
                db, coll.name, locale=settings.default_locale, limit=per_collection
            )
            if _is_indexable(e)
        ]
        if coll.name == "page":
            items = [e for e in items if e.live_slug not in ("privacy", "terms", "cookies")]
        if not items:
            continue
        out += [f"## {coll.label}", ""]
        for e in items:
            summary = (e.live or {}).get("summary", "").strip().replace("\n", " ")
            out.append(
                f"- [{e.live['title']}]({_entry_url(settings, e)})"
                + (f": {summary[:200]}" if summary else "")
            )
        out.append("")
    legal = [
        e
        for e in live_entries(db, "page", limit=200)
        if e.live_slug in ("privacy", "terms", "cookies")
    ]
    if legal:
        out += (
            ["## Optional", ""]
            + [f"- [{e.live['title']}]({_entry_url(settings, e)})" for e in legal]
            + [""]
        )
    return "\n".join(out)


def rss_feed(db: Session, settings: Settings, collection: str = "post", limit: int = 30) -> str:
    items = []
    for e in live_entries(db, collection, locale=settings.default_locale, limit=limit):
        url = _entry_url(settings, e)
        date = e.published_at.strftime("%a, %d %b %Y %H:%M:%S +0000") if e.published_at else ""
        items.append(
            f"<item><title>{xml_escape(e.live['title'])}</title><link>{xml_escape(url)}</link>"
            f'<guid isPermaLink="true">{xml_escape(url)}</guid><pubDate>{date}</pubDate>'
            f"<description>{xml_escape(e.live.get('summary', ''))}</description></item>"
        )
    return (
        '<?xml version="1.0" encoding="UTF-8"?><rss version="2.0"><channel>'
        f"<title>{xml_escape(settings.site_name)}</title>"
        f"<link>{xml_escape(settings.base_url)}</link>"
        f"<description>{xml_escape(settings.site_name)}</description>"
        + "".join(items)
        + "</channel></rss>"
    )


def og_image_svg(title: str, site_name: str, accent: str = "#1f4fd1") -> str:
    """A 1200×630 share image rendered from the page title (no external services)."""
    lines = textwrap.wrap(title, 26)[:4] or [site_name]
    tspans = "".join(
        f'<tspan x="80" dy="{0 if i == 0 else 84}">{html.escape(t)}</tspan>'
        for i, t in enumerate(lines)
    )
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" width="1200" height="630" viewBox="0 0 1200 630">'
        '<rect width="1200" height="630" fill="#0f1115"/>'
        f'<rect x="0" y="0" width="24" height="630" fill="{html.escape(accent)}"/>'
        '<text x="80" y="200" font-family="system-ui,Segoe UI,Roboto,sans-serif" '
        f'font-size="72" font-weight="700" fill="#ffffff">{tspans}</text>'
        '<text x="80" y="570" font-family="system-ui,sans-serif" font-size="36" '
        f'fill="#a3a9b5">{html.escape(site_name)}</text></svg>'
    )
