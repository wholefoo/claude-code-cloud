"""Collections: each content type has its own schema, URL pattern, template and schema.org type.

Sites register more with :func:`register_collection`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator
from redblue.core.security import is_safe_link


class _Data(BaseModel):
    model_config = ConfigDict(extra="forbid")


class NoData(_Data):
    pass


class PageData(_Data):
    page_type: str = "about"  # which template to render (see redblue.templates.registry)


class PostData(_Data):
    reading_minutes: int | None = None
    series: str | None = None


class ProductData(_Data):
    price: str | None = None
    currency: str = "USD"
    sku: str | None = None
    availability: str = "InStock"
    image_key: str | None = None
    brand: str | None = None


class ComparisonData(_Data):
    product_a: str
    product_b: str | None = None
    disclosure: str = Field(
        default="We make one of the products compared here. We have tried to be fair.",
        description="Required honesty disclosure for comparison pages.",
    )


class LocationData(_Data):
    name: str
    street: str | None = None
    city: str
    region: str | None = None
    postal_code: str | None = None
    country: str = "US"
    phone: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    service_areas: list[str] = Field(default_factory=list)
    opening_hours: list[str] = Field(default_factory=list)  # e.g. "Mo-Fr 09:00-17:00"


class JobData(_Data):
    employment_type: str = "FULL_TIME"
    location: str = "Remote"
    remote: bool = True
    salary_min: int | None = None
    salary_max: int | None = None
    currency: str = "USD"
    valid_through: date | None = None
    apply_url: str = "/contact"

    @field_validator("apply_url")
    @classmethod
    def _url(cls, v: str) -> str:
        if not is_safe_link(v):
            raise ValueError("Unsafe apply URL.")
        return v


class EventData(_Data):
    start: datetime
    end: datetime | None = None
    online: bool = True
    location: str | None = None
    register_url: str = "/contact"


class CaseStudyData(_Data):
    customer: str
    industry: str | None = None
    results: list[str] = Field(default_factory=list)


class GlossaryData(_Data):
    short_definition: str = Field(max_length=300)
    also_known_as: list[str] = Field(default_factory=list)


class ChangelogData(_Data):
    version: str | None = None
    released: date | None = None


class IntegrationData(_Data):
    partner: str
    partner_url: str | None = None


class DirectoryData(_Data):
    name: str
    website: str | None = None
    city: str | None = None
    category: str | None = None


@dataclass(frozen=True)
class CollectionDef:
    name: str
    label: str
    url_prefix: str  # "" for top-level pages
    page_type: str  # default template key
    listing_page_type: str | None
    schema_type: str
    data_model: type[BaseModel] = NoData
    programmatic: bool = False  # stricter uniqueness/quality checks before publishing
    in_llms_txt: bool = True
    changefreq: str = "weekly"
    priority: float = 0.6
    extra: dict = field(default_factory=dict)

    def path_for(self, slug: str, locale: str = "en", default_locale: str = "en") -> str:
        prefix = "" if locale == default_locale else f"/{locale}"
        if self.name == "page" and slug == "home":
            return prefix + "/"
        return f"{prefix}{self.url_prefix}/{slug}"

    def listing_path(self, locale: str = "en", default_locale: str = "en") -> str | None:
        if not self.listing_page_type or not self.url_prefix:
            return None
        prefix = "" if locale == default_locale else f"/{locale}"
        return f"{prefix}{self.url_prefix}"


COLLECTIONS: dict[str, CollectionDef] = {}


def register_collection(c: CollectionDef) -> CollectionDef:
    COLLECTIONS[c.name] = c
    return c


for _c in [
    CollectionDef("page", "Pages", "", "about", None, "WebPage", PageData, priority=0.8),
    CollectionDef(
        "post",
        "Blog posts",
        "/blog",
        "blog_post",
        "blog_index",
        "BlogPosting",
        PostData,
        changefreq="monthly",
    ),
    CollectionDef(
        "doc", "Documentation", "/docs", "documentation", "knowledge_base", "TechArticle"
    ),
    CollectionDef("guide", "How-to guides", "/guides", "howto", "blog_index", "HowTo"),
    CollectionDef("question", "Q&A", "/questions", "qa_article", "faq", "QAPage"),
    CollectionDef(
        "glossary",
        "Glossary",
        "/glossary",
        "glossary_term",
        "glossary_index",
        "DefinedTerm",
        GlossaryData,
    ),
    CollectionDef(
        "product",
        "Products",
        "/products",
        "product",
        "product_listing",
        "Product",
        ProductData,
        priority=0.7,
    ),
    CollectionDef(
        "comparison", "Comparisons", "/compare", "comparison", None, "Article", ComparisonData
    ),
    CollectionDef(
        "alternative",
        "Alternatives",
        "/alternatives",
        "alternatives",
        None,
        "Article",
        ComparisonData,
    ),
    CollectionDef("use_case", "Use cases", "/use-cases", "use_case", None, "WebPage"),
    CollectionDef("industry", "Industries", "/industries", "industry", None, "WebPage"),
    CollectionDef(
        "integration",
        "Integrations",
        "/integrations",
        "integration",
        None,
        "SoftwareApplication",
        IntegrationData,
    ),
    CollectionDef(
        "case_study",
        "Case studies",
        "/customers",
        "case_study",
        "portfolio",
        "Article",
        CaseStudyData,
    ),
    CollectionDef(
        "location",
        "Locations",
        "/locations",
        "location",
        "directory_index",
        "LocalBusiness",
        LocationData,
        programmatic=True,
    ),
    CollectionDef(
        "service_area",
        "Service areas",
        "/areas",
        "service_area",
        None,
        "Service",
        LocationData,
        programmatic=True,
    ),
    CollectionDef(
        "directory",
        "Directory",
        "/directory",
        "directory_listing",
        "directory_index",
        "LocalBusiness",
        DirectoryData,
        programmatic=True,
    ),
    CollectionDef(
        "job",
        "Jobs",
        "/careers",
        "job_posting",
        "careers",
        "JobPosting",
        JobData,
        in_llms_txt=False,
    ),
    CollectionDef("event", "Events", "/events", "event", "event_listing", "Event", EventData),
    CollectionDef(
        "changelog",
        "Changelog",
        "/changelog",
        "changelog_entry",
        "changelog",
        "Article",
        ChangelogData,
        changefreq="daily",
    ),
    CollectionDef(
        "newsletter",
        "Newsletter archive",
        "/newsletter",
        "blog_post",
        "newsletter_archive",
        "Article",
        in_llms_txt=False,
    ),
    CollectionDef("feature", "Features", "/features", "feature_detail", "features", "WebPage"),
]:
    register_collection(_c)


def get_collection(name: str) -> CollectionDef:
    try:
        return COLLECTIONS[name]
    except KeyError:
        raise KeyError(f"Unknown collection {name!r}") from None


def validate_data(collection: str, data: dict) -> dict:
    model = get_collection(collection).data_model
    return model.model_validate(data or {}).model_dump(mode="json", exclude_none=True)
