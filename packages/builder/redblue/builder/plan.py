"""Plan step: intent → a BuildPlan the user approves before anything is generated."""

from __future__ import annotations

import re

from pydantic import BaseModel, Field, field_validator

from redblue.templates.registry import CATALOG

BUILDER_SYSTEM = """You are the RedBlue builder agent. Turn a site idea into a concise build
plan using ONLY page types from the provided catalog. Prefer fewer, stronger pages over many
thin ones. Programmatic pages (locations, directories) are only appropriate when each page
will have genuinely unique content. Include the questions the audience asks, so the content
can answer them directly."""


class PlannedPage(BaseModel):
    page_type: str
    title: str = Field(max_length=120)
    slug: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,80}$")
    purpose: str = Field(max_length=300)

    @field_validator("page_type")
    @classmethod
    def _known(cls, v: str) -> str:
        if v not in CATALOG:
            raise ValueError(f"Unknown page type {v!r}")
        return v


class BuildPlan(BaseModel):
    site_name: str = Field(max_length=80)
    description: str = Field(max_length=300)
    audience: str = Field(default="", max_length=300)
    pages: list[PlannedPage]
    collections: list[str] = Field(default_factory=list)
    features: list[str] = Field(default_factory=list)
    questions: list[str] = Field(default_factory=list, max_length=30)
    primary_color: str = Field(default="#1f4fd1", pattern=r"^#[0-9a-fA-F]{6}$")

    def summary(self) -> str:
        lines = [f"Site: {self.site_name}: {self.description}", "Pages:"]
        lines += [
            f"  - {p.title} ({CATALOG[p.page_type].name}) /{p.slug}: {p.purpose}"
            for p in self.pages
        ]
        if self.collections:
            lines.append(f"Collections: {', '.join(self.collections)}")
        if self.features:
            lines.append(f"Features: {', '.join(self.features)}")
        if self.questions:
            lines.append("Questions to answer: " + "; ".join(self.questions[:8]))
        return "\n".join(lines)


RULES: list[tuple[str, list[tuple[str, str, str, str]], list[str], list[str]]] = [
    # (keyword regex, pages [(page_type, title, slug, purpose)], collections, features)
    (
        r"\bblog|articles?|posts?|writ",
        [("blog_index", "Blog", "blog", "Latest articles")],
        ["post"],
        ["newsletter"],
    ),
    (
        r"\bdocs?|documentation|api|developer",
        [("knowledge_base", "Documentation", "docs", "Guides and reference")],
        ["doc", "guide"],
        [],
    ),
    (
        r"\bsaas|software|app\b|platform|tool|startup",
        [
            ("pricing", "Pricing", "pricing", "Plans and FAQs"),
            ("features", "Features", "features", "What the product does"),
        ],
        ["feature", "integration", "comparison", "changelog"],
        ["waitlist"],
    ),
    (
        r"\b(shop|store|e-?commerce|sell|products)\b",
        [("product_listing", "Products", "products", "Product catalog")],
        ["product"],
        [],
    ),
    (
        r"\blocal|plumb|restaurant|clinic|salon|dentist|lawyer|near me|city|cities|service area",
        [("location", "Locations", "locations", "Where to find us")],
        ["location"],
        [],
    ),
    (
        r"\bportfolio|agency|studio|freelanc|consult",
        [
            ("portfolio", "Work", "work", "Selected projects"),
            ("case_study", "Case studies", "customers", "Customer results"),
        ],
        ["case_study"],
        [],
    ),
    (
        r"\bevents?|webinars?|conference|meetup",
        [("event_listing", "Events", "events", "Upcoming events")],
        ["event"],
        [],
    ),
    (r"\bhiring|careers?|jobs?", [("careers", "Careers", "careers", "Open roles")], ["job"], []),
    (
        r"\bfaq|questions|help|support",
        [("faq", "FAQ", "faq", "Answers to common questions")],
        ["question"],
        [],
    ),
    (
        r"\bglossary|terms|definitions",
        [("glossary_index", "Glossary", "glossary", "Definitions of key terms")],
        ["glossary"],
        [],
    ),
    (r"\bnewsletter|subscribe|mailing", [], [], ["newsletter"]),
]


def heuristic_plan(intent: str, site_name: str | None = None) -> BuildPlan:
    text = intent.lower()
    pages = [
        PlannedPage(
            page_type="home",
            title="Home",
            slug="home",
            purpose="Explain the offer and route visitors",
        )
    ]
    collections, features = [], ["contact"]
    for pattern, ps, cs, fs in RULES:
        if re.search(pattern, text):
            pages += [
                PlannedPage(page_type=t, title=ti, slug=s, purpose=pu)
                for t, ti, s, pu in ps
                if all(p.slug != s for p in pages)
            ]
            collections += [c for c in cs if c not in collections]
            features += [f for f in fs if f not in features]
    pages += [
        PlannedPage(
            page_type="about", title="About", slug="about", purpose="Who is behind the site"
        ),
        PlannedPage(
            page_type="contact", title="Contact", slug="contact", purpose="How to get in touch"
        ),
        PlannedPage(
            page_type="privacy",
            title="Privacy policy",
            slug="privacy",
            purpose="How personal data is handled",
        ),
    ]
    name = site_name or (re.sub(r"[^A-Za-z0-9 ]", "", intent).strip().title()[:40] or "My Site")
    return BuildPlan(
        site_name=name,
        description=intent.strip()[:300],
        pages=pages,
        collections=collections,
        features=features,
        questions=[f"What is {name}?", f"Who is {name} for?"],
    )


def make_plan(
    intent: str, ai=None, model: str = "claude-sonnet-5", site_name: str | None = None
) -> BuildPlan:
    if ai is not None and getattr(ai, "available", False):
        from redblue.core.ai import untrusted

        catalog = "\n".join(f"- {k}: {pt.name} ({pt.category})" for k, pt in CATALOG.items())
        return ai.structured(
            agent="builder",
            model=model,
            system=BUILDER_SYSTEM,
            output=BuildPlan,
            task="builder.plan",
            prompt=f"Page type catalog:\n{catalog}\n\n"
            f"Preferred site name: {site_name or 'choose one'}\n\n" + untrusted("intent", intent),
        )
    return heuristic_plan(intent, site_name)
