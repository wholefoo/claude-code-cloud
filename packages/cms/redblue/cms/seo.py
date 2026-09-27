"""The SEO/AEO panel attached to every entry."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator

from redblue.core.security import is_safe_link

SCHEMA_TYPES = (
    "WebPage",
    "Article",
    "BlogPosting",
    "NewsArticle",
    "TechArticle",
    "FAQPage",
    "HowTo",
    "QAPage",
    "Product",
    "SoftwareApplication",
    "Service",
    "LocalBusiness",
    "Organization",
    "Person",
    "Event",
    "JobPosting",
    "DefinedTerm",
    "CollectionPage",
    "AboutPage",
    "ContactPage",
    "ItemList",
    "Review",
)


class SEOFields(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(default=None, max_length=120)
    description: str | None = Field(default=None, max_length=320)
    canonical: str | None = None
    schema_type: str | None = None
    og_image_key: str | None = None
    noindex: bool = False
    target_questions: list[str] = Field(default_factory=list, max_length=30)
    keywords: list[str] = Field(default_factory=list, max_length=30)

    @field_validator("canonical")
    @classmethod
    def _canonical(cls, v: str | None) -> str | None:
        if v and (not is_safe_link(v) or v.startswith(("mailto:", "tel:"))):
            raise ValueError("Canonical must be a relative path or http(s) URL.")
        return v

    @field_validator("schema_type")
    @classmethod
    def _schema(cls, v: str | None) -> str | None:
        if v and v not in SCHEMA_TYPES:
            raise ValueError(f"Unknown schema type {v!r}.")
        return v


def seo_warnings(title: str, seo: SEOFields, summary: str) -> list[str]:
    """Non-blocking hints shown in the editor's SEO panel."""
    warnings: list[str] = []
    t = seo.title or title
    d = seo.description or summary
    if len(t) < 15:
        warnings.append("Title is short; aim for 30–60 characters.")
    if len(t) > 60:
        warnings.append("Title may be truncated in search results (over 60 characters).")
    if not d:
        warnings.append("Add a meta description (or summary).")
    elif not 70 <= len(d) <= 160:
        warnings.append("Meta description works best at 70–160 characters.")
    if not seo.target_questions:
        warnings.append("Add target questions so the AEO audit can check coverage.")
    return warnings
