"""The page-type catalog (plan §4.4). Each page type names its layout template, schema.org
type and Core Web Vitals budget. Page types that share a layout differ by variant, JSON-LD
and the collection that feeds them."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class CWVBudget:
    lcp_ms: int = 2500
    cls: float = 0.1
    inp_ms: int = 200
    max_html_kb: int = 100
    max_js_kb: int = 60  # htmx + rb.js
    max_css_kb: int = 40


@dataclass(frozen=True)
class PageType:
    key: str
    name: str
    category: str
    template: str
    schema_type: str = "WebPage"
    collection: str | None = None  # entries of this collection render with this type
    listing_of: str | None = None  # listing pages: which collection they list
    description: str = ""
    budget: CWVBudget = field(default_factory=CWVBudget)


CATALOG: dict[str, PageType] = {}


def _add(
    key,
    name,
    category,
    template,
    schema="WebPage",
    collection=None,
    listing_of=None,
    description="",
):
    CATALOG[key] = PageType(
        key, name, category, f"pages/{template}.html", schema, collection, listing_of, description
    )


# Core & marketing
_add("home", "Home", "core", "home", "WebPage", "page")
_add("landing", "Landing page", "core", "landing", "WebPage", "page")
_add("landing_split", "Landing page (split hero)", "core", "landing", "WebPage", "page")
_add("landing_minimal", "Landing page (minimal)", "core", "landing", "WebPage", "page")
_add("about", "About", "core", "article", "AboutPage", "page")
_add("contact", "Contact", "core", "form_page", "ContactPage", "page")
_add("pricing", "Pricing", "core", "landing", "WebPage", "page")
_add("features", "Features", "core", "listing", "CollectionPage", listing_of="feature")
_add("feature_detail", "Feature detail", "core", "article", "WebPage", "feature")
_add("waitlist", "Waitlist / coming soon", "core", "form_page", "WebPage", "page")
_add("thank_you", "Thank-you / confirmation", "core", "message", "WebPage", "page")
_add("link_in_bio", "Link-in-bio", "core", "link_in_bio", "ProfilePage", "page")
# Content & editorial
_add("blog_index", "Blog index", "content", "listing", "Blog", listing_of="post")
_add("blog_post", "Blog post", "content", "article", "BlogPosting", "post")
_add("archive", "Category / tag archive", "content", "listing", "CollectionPage")
_add("author", "Author profile", "content", "author", "ProfilePage")
_add(
    "newsletter_archive",
    "Newsletter archive",
    "content",
    "listing",
    "CollectionPage",
    listing_of="newsletter",
)
_add("series", "Series / collection page", "content", "listing", "CollectionPage")
# Answer & knowledge (AEO)
_add("faq", "FAQ", "answer", "listing", "FAQPage", listing_of="question")
_add(
    "knowledge_base",
    "Knowledge base / help center",
    "answer",
    "listing",
    "CollectionPage",
    listing_of="doc",
)
_add("documentation", "Documentation", "answer", "docs", "TechArticle", "doc")
_add(
    "glossary_index",
    "Glossary index",
    "answer",
    "glossary_index",
    "DefinedTermSet",
    listing_of="glossary",
)
_add("glossary_term", "Glossary term", "answer", "article", "DefinedTerm", "glossary")
_add("howto", "How-to / tutorial", "answer", "article", "HowTo", "guide")
_add("qa_article", "Q&A article", "answer", "article", "QAPage", "question")
# Commercial & comparison
_add("product", "Product page", "commercial", "product", "Product", "product")
_add(
    "product_listing", "Product listing", "commercial", "listing", "ItemList", listing_of="product"
)
_add("comparison", '"X vs Y" comparison', "commercial", "article", "Article", "comparison")
_add("alternatives", '"Alternatives to X"', "commercial", "article", "Article", "alternative")
_add("use_case", "Use-case page", "commercial", "article", "WebPage", "use_case")
_add("industry", "Industry / solution page", "commercial", "article", "WebPage", "industry")
_add(
    "integration", "Integration page", "commercial", "article", "SoftwareApplication", "integration"
)
_add("case_study", "Case study", "commercial", "article", "Article", "case_study")
_add("testimonials", "Testimonials / reviews", "commercial", "landing", "WebPage", "page")
# Lead generation
_add("lead_magnet", "Lead magnet / download", "lead", "form_page", "WebPage", "page")
_add("webinar", "Webinar / event", "lead", "event", "Event", "event")
_add("event_listing", "Event listing", "lead", "listing", "ItemList", listing_of="event")
_add("newsletter_signup", "Newsletter signup", "lead", "form_page", "WebPage", "page")
_add("free_tool", "Free tool / calculator page", "lead", "tool", "WebApplication", "page")
_add("quiz", "Quiz / assessment", "lead", "tool", "WebPage", "page")
# Local & programmatic
_add("location", "Location page", "local", "location", "LocalBusiness", "location")
_add("service_area", "Service-area page", "local", "location", "Service", "service_area")
_add("directory_listing", "Directory listing", "local", "location", "LocalBusiness", "directory")
_add("directory_index", "Directory index", "local", "listing", "ItemList", listing_of="directory")
# Company & trust
_add("careers", "Careers", "company", "listing", "CollectionPage", listing_of="job")
_add("job_posting", "Job posting", "company", "job", "JobPosting", "job")
_add("changelog", "Changelog", "company", "listing", "CollectionPage", listing_of="changelog")
_add("changelog_entry", "Changelog entry", "company", "article", "Article", "changelog")
_add("roadmap", "Roadmap", "company", "article", "WebPage", "page")
_add("press", "Press / media kit", "company", "article", "WebPage", "page")
_add("team", "Team", "company", "team", "AboutPage", "page")
_add("portfolio", "Portfolio", "company", "listing", "CollectionPage", listing_of="case_study")
_add("status", "Status page", "company", "status", "WebPage", "page")
# Utility
_add("search_results", "Search results", "utility", "search", "SearchResultsPage")
_add("not_found", "404", "utility", "message", "WebPage")
_add("maintenance", "Maintenance", "utility", "message", "WebPage")
_add("privacy", "Privacy policy", "utility", "legal", "WebPage", "page")
_add("terms", "Terms of service", "utility", "legal", "WebPage", "page")
_add("cookies", "Cookie policy", "utility", "legal", "WebPage", "page")
_add("account", "Account / dashboard shell", "utility", "account", "WebPage")
_add("login", "Login / signup", "utility", "login", "WebPage")

CATEGORIES = {
    "core": "Core & marketing",
    "content": "Content & editorial",
    "answer": "Answer & knowledge (AEO)",
    "commercial": "Commercial & comparison",
    "lead": "Lead generation",
    "local": "Local & programmatic",
    "company": "Company & trust",
    "utility": "Utility",
}


def get_page_type(key: str) -> PageType:
    return CATALOG[key]


def page_type_for_collection(collection: str, override: str | None = None) -> PageType:
    if override and override in CATALOG:
        return CATALOG[override]
    from redblue.cms.collections import get_collection

    return CATALOG[get_collection(collection).page_type]


def listing_type_for(collection: str) -> PageType | None:
    from redblue.cms.collections import get_collection

    key = get_collection(collection).listing_page_type
    return CATALOG.get(key) if key else None


def by_category() -> dict[str, list[PageType]]:
    out: dict[str, list[PageType]] = {k: [] for k in CATEGORIES}
    for pt in CATALOG.values():
        out[pt.category].append(pt)
    return out
