"""SEO/AEO audit of rendered pages: titles, descriptions, headings, canonical, structured data,
images, internal links, orphans, page weight vs. Core Web Vitals budgets."""

from __future__ import annotations

import json
from html.parser import HTMLParser
from urllib.parse import urlsplit

from pydantic import BaseModel, Field

from redblue.templates.jsonld import validate_graph
from redblue.templates.registry import CWVBudget


class Issue(BaseModel):
    path: str
    severity: str  # error / warning / notice
    check: str
    message: str


class PageAudit(BaseModel):
    path: str
    status: int
    title: str = ""
    html_kb: float = 0.0
    internal_links: list[str] = Field(default_factory=list)
    issues: list[Issue] = Field(default_factory=list)


class AuditReport(BaseModel):
    pages: list[PageAudit]
    orphans: list[str] = Field(default_factory=list)
    broken_links: list[dict] = Field(default_factory=list)
    score: float = 0.0

    @property
    def issues(self) -> list[Issue]:
        return [i for p in self.pages for i in p.issues]


class _Parser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.title, self.in_title = "", False
        self.meta: dict[str, str] = {}
        self.links: list[str] = []
        self.canonical = ""
        self.h1 = 0
        self.headings: list[int] = []
        self.imgs: list[dict] = []
        self.lang = ""
        self.jsonld: list[str] = []
        self._in_ld = False
        self._buf = ""

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "html":
            self.lang = a.get("lang", "")
        elif tag == "title":
            self.in_title = True
        elif tag == "meta" and (a.get("name") or a.get("property")):
            self.meta[(a.get("name") or a.get("property")).lower()] = a.get("content", "")
        elif tag == "link" and a.get("rel") == "canonical":
            self.canonical = a.get("href", "")
        elif tag == "a" and a.get("href"):
            self.links.append(a["href"])
        elif tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            self.headings.append(int(tag[1]))
            self.h1 += tag == "h1"
        elif tag == "img":
            self.imgs.append(a)
        elif tag == "script" and a.get("type") == "application/ld+json":
            self._in_ld, self._buf = True, ""

    def handle_endtag(self, tag):
        if tag == "title":
            self.in_title = False
        elif tag == "script" and self._in_ld:
            self._in_ld = False
            self.jsonld.append(self._buf)

    def handle_data(self, data):
        if self.in_title:
            self.title += data
        if self._in_ld:
            self._buf += data


def audit_html(path: str, status: int, html: str, budget: CWVBudget | None = None) -> PageAudit:
    budget = budget or CWVBudget()
    p = _Parser()
    p.feed(html)
    page = PageAudit(
        path=path, status=status, title=p.title.strip(), html_kb=round(len(html.encode()) / 1024, 1)
    )

    def issue(sev, check, msg):
        page.issues.append(Issue(path=path, severity=sev, check=check, message=msg))

    if status >= 400:
        issue("error", "status", f"HTTP {status}")
        return page
    t = page.title
    if not t:
        issue("error", "title", "Missing <title>.")
    elif not 15 <= len(t) <= 65:
        issue("warning", "title", f"Title is {len(t)} characters (aim for 15–65).")
    desc = p.meta.get("description", "")
    if not desc:
        issue("warning", "meta-description", "Missing meta description.")
    elif not 50 <= len(desc) <= 170:
        issue("notice", "meta-description", f"Description is {len(desc)} characters.")
    if p.h1 != 1:
        issue("error" if p.h1 == 0 else "warning", "h1", f"Page has {p.h1} <h1> elements.")
    for prev, cur in zip(p.headings, p.headings[1:], strict=False):
        if cur > prev + 1:
            issue("notice", "heading-order", f"Heading jumps from h{prev} to h{cur}.")
            break
    if not p.canonical:
        issue("warning", "canonical", "Missing canonical link.")
    if not p.lang:
        issue("error", "lang", "Missing <html lang>.")
    if "og:title" not in p.meta:
        issue("notice", "open-graph", "Missing og:title.")
    for img in p.imgs:
        if "alt" not in img:
            issue("error", "img-alt", f"Image {img.get('src', '')[:60]} has no alt attribute.")
        if not (img.get("width") and img.get("height")):
            issue(
                "notice",
                "img-dimensions",
                f"Image {img.get('src', '')[:60]} lacks width/height (layout shift risk).",
            )
    if not p.jsonld:
        issue("warning", "structured-data", "No JSON-LD structured data.")
    for raw in p.jsonld:
        try:
            for prob in validate_graph(json.loads(raw)):
                issue("warning", "structured-data", prob)
        except json.JSONDecodeError:
            issue("error", "structured-data", "Invalid JSON-LD.")
    if page.html_kb > budget.max_html_kb:
        issue(
            "warning", "page-weight", f"HTML is {page.html_kb} KB (budget {budget.max_html_kb} KB)."
        )
    page.internal_links = sorted(
        {
            urlsplit(h).path
            for h in p.links
            if (h.startswith("/") and not h.startswith("//")) and not h.startswith("/_rb")
        }
    )
    return page


def audit_site(
    client,
    paths: list[str],
    budgets: dict[str, CWVBudget] | None = None,
    nav_paths: set[str] | None = None,
) -> AuditReport:
    """Crawl ``paths`` (and internal links found on them, one level) with a same-origin client
    (TestClient or httpx.Client bound to the site)."""
    pages: dict[str, PageAudit] = {}
    queue = list(dict.fromkeys(paths))
    linked_from: dict[str, set[str]] = {}
    while queue and len(pages) < 500:
        path = queue.pop(0)
        if path in pages:
            continue
        resp = client.get(path, follow_redirects=True)
        ctype = resp.headers.get("content-type", "")
        html = resp.text if "html" in ctype else ""
        pages[path] = (
            audit_html(path, resp.status_code, html, (budgets or {}).get(path))
            if html or resp.status_code >= 400
            else PageAudit(path=path, status=resp.status_code)
        )
        for link in pages[path].internal_links:
            linked_from.setdefault(link, set()).add(path)
            if link not in pages and link in paths:
                queue.append(link)
    broken = []
    for link, sources in linked_from.items():
        if link in pages:
            status = pages[link].status
        else:
            status = client.get(link, follow_redirects=True).status_code
        if status >= 400:
            broken.append({"link": link, "status": status, "from": sorted(sources)[:5]})
    nav_paths = nav_paths or set()
    orphans = [
        p
        for p in paths
        if p != "/" and p not in nav_paths and not (linked_from.get(p, set()) - {p})
    ]
    report = AuditReport(pages=list(pages.values()), orphans=orphans, broken_links=broken)
    weights = {"error": 5, "warning": 2, "notice": 0.5}
    penalty = sum(weights[i.severity] for i in report.issues) + 5 * len(broken)
    report.score = round(max(0.0, 100 - penalty / max(1, len(pages)) * 5), 1)
    return report
