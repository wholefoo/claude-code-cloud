"""Content blocks: structured JSON validated by Pydantic, never raw HTML.

Every link is checked with :func:`is_safe_link`, every image requires alt text (or an
explicit ``decorative`` flag), and embeds are limited to an allowlist of providers.
"""

from __future__ import annotations

import re
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from redblue.core.security import is_safe_link


def _check_link(v: str) -> str:
    v = v.strip()
    if not is_safe_link(v):
        raise ValueError(f"Unsafe or invalid link: {v!r}")
    return v


INLINE_LINK = re.compile(r"\[([^\]]+)\]\(([^)\s]+)\)")


def _check_inline(v: str) -> str:
    for _, url in INLINE_LINK.findall(v):
        _check_link(url)
    return v


class _Block(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Heading(_Block):
    type: Literal["heading"] = "heading"
    level: Literal[2, 3, 4] = 2
    text: str = Field(min_length=1, max_length=200)
    anchor: str | None = Field(default=None, pattern=r"^[a-z0-9-]{1,80}$")


class Paragraph(_Block):
    """Inline markup: **bold**, *italic*, `code`, [text](url). Everything else is escaped."""

    type: Literal["paragraph"] = "paragraph"
    text: str = Field(min_length=1, max_length=10000)
    _v = field_validator("text")(_check_inline)


class ListBlock(_Block):
    type: Literal["list"] = "list"
    ordered: bool = False
    items: list[str] = Field(min_length=1, max_length=100)

    @field_validator("items")
    @classmethod
    def _links(cls, v: list[str]) -> list[str]:
        return [_check_inline(i) for i in v]


class Image(_Block):
    type: Literal["image"] = "image"
    media_key: str | None = None
    src: str | None = None
    alt: str = Field(default="", max_length=300)
    decorative: bool = False
    caption: str | None = None
    width: int | None = None
    height: int | None = None

    @model_validator(mode="after")
    def _require(self) -> Image:
        if not (self.media_key or self.src):
            raise ValueError("Image needs media_key or src.")
        if self.src:
            _check_link(self.src)
        if not self.decorative and not self.alt.strip():
            raise ValueError("Image alt text is required (or mark it decorative).")
        return self


class Quote(_Block):
    type: Literal["quote"] = "quote"
    text: str
    cite: str | None = None


class Code(_Block):
    type: Literal["code"] = "code"
    language: str = Field(default="text", pattern=r"^[a-z0-9+#-]{1,20}$")
    code: str


class Callout(_Block):
    type: Literal["callout"] = "callout"
    tone: Literal["info", "warning", "success", "danger"] = "info"
    text: str
    _v = field_validator("text")(_check_inline)


class Answer(_Block):
    """Answer-first block (AEO): a concise direct answer, then optional depth."""

    type: Literal["answer"] = "answer"
    question: str = Field(min_length=3, max_length=300)
    answer: str = Field(min_length=1, max_length=1200)
    detail: str | None = None


class QA(BaseModel):
    question: str = Field(min_length=3, max_length=300)
    answer: str = Field(min_length=1, max_length=5000)
    _v = field_validator("answer")(_check_inline)


class FAQ(_Block):
    type: Literal["faq"] = "faq"
    items: list[QA] = Field(min_length=1, max_length=50)


class HowToStep(BaseModel):
    name: str
    text: str
    image_key: str | None = None


class HowTo(_Block):
    type: Literal["howto"] = "howto"
    name: str
    total_time: str | None = Field(default=None, pattern=r"^P(T?\d+[HMSD])+$")  # ISO 8601
    supplies: list[str] = Field(default_factory=list)
    steps: list[HowToStep] = Field(min_length=1)


class CTA(_Block):
    type: Literal["cta"] = "cta"
    heading: str
    text: str = ""
    button_label: str
    button_url: str
    _v = field_validator("button_url")(_check_link)


class Embed(_Block):
    type: Literal["embed"] = "embed"
    provider: Literal["youtube", "vimeo"]
    video_id: str = Field(pattern=r"^[A-Za-z0-9_-]{6,20}$")
    title: str = Field(min_length=1, max_length=200)


class Table(_Block):
    type: Literal["table"] = "table"
    caption: str | None = None
    headers: list[str]
    rows: list[list[str]]


class Divider(_Block):
    type: Literal["divider"] = "divider"


class Feature(BaseModel):
    title: str
    text: str
    url: str | None = None
    _v = field_validator("url")(lambda v: _check_link(v) if v else v)


class Features(_Block):
    type: Literal["features"] = "features"
    heading: str | None = None
    items: list[Feature] = Field(min_length=1)


class Plan(BaseModel):
    name: str
    price: str
    currency: str = "USD"
    period: str = "month"
    description: str = ""
    features: list[str] = Field(default_factory=list)
    cta_label: str = "Get started"
    cta_url: str = "/contact"
    highlighted: bool = False
    _v = field_validator("cta_url")(_check_link)


class Pricing(_Block):
    type: Literal["pricing"] = "pricing"
    plans: list[Plan] = Field(min_length=1, max_length=6)


class Testimonial(_Block):
    """Only real, attributable testimonials. ``verified`` must be set by an editor before
    the testimonial contributes Review structured data (no fake reviews)."""

    type: Literal["testimonial"] = "testimonial"
    quote: str
    author: str
    role: str | None = None
    rating: int | None = Field(default=None, ge=1, le=5)
    verified: bool = False
    source_url: str | None = None
    _v = field_validator("source_url")(lambda v: _check_link(v) if v else v)


class Stat(BaseModel):
    value: str
    label: str


class Stats(_Block):
    type: Literal["stats"] = "stats"
    items: list[Stat] = Field(min_length=1, max_length=8)


class FormBlock(_Block):
    type: Literal["form"] = "form"
    kind: Literal["newsletter", "contact", "waitlist", "lead_magnet"]
    heading: str | None = None
    button_label: str = "Submit"
    lead_magnet_key: str | None = None


class ComparisonRow(BaseModel):
    feature: str
    values: list[str]


class Comparison(_Block):
    type: Literal["comparison"] = "comparison"
    columns: list[str] = Field(min_length=2)
    rows: list[ComparisonRow] = Field(min_length=1)
    verdict: str | None = None


class Source(BaseModel):
    title: str
    url: str
    _v = field_validator("url")(_check_link)


class Sources(_Block):
    """Citations (AEO source signal)."""

    type: Literal["sources"] = "sources"
    items: list[Source] = Field(min_length=1)


Block = Annotated[
    Heading
    | Paragraph
    | ListBlock
    | Image
    | Quote
    | Code
    | Callout
    | Answer
    | FAQ
    | HowTo
    | CTA
    | Embed
    | Table
    | Divider
    | Features
    | Pricing
    | Testimonial
    | Stats
    | FormBlock
    | Comparison
    | Sources,
    Field(discriminator="type"),
]


class BlockList(BaseModel):
    blocks: list[Block] = Field(default_factory=list)


def validate_blocks(data: list[dict]) -> list[dict]:
    """Validate raw block JSON and return the normalised form stored in the database."""
    return [b.model_dump(exclude_none=True) for b in BlockList(blocks=data).blocks]


BLOCK_TYPES = sorted(
    t.model_fields["type"].default
    for t in (
        Heading,
        Paragraph,
        ListBlock,
        Image,
        Quote,
        Code,
        Callout,
        Answer,
        FAQ,
        HowTo,
        CTA,
        Embed,
        Table,
        Divider,
        Features,
        Pricing,
        Testimonial,
        Stats,
        FormBlock,
        Comparison,
        Sources,
    )
)


def blocks_text(blocks: list[dict]) -> str:
    """Plain text for search, word counts, similarity and llms.txt summaries."""
    out: list[str] = []

    def add(*parts):
        out.extend(p for p in parts if p)

    for b in blocks:
        t = b.get("type")
        if t in ("heading", "paragraph", "callout", "quote"):
            add(b.get("text"))
        elif t == "list":
            add(*b.get("items", []))
        elif t == "answer":
            add(b.get("question"), b.get("answer"), b.get("detail"))
        elif t == "faq":
            for it in b.get("items", []):
                add(it.get("question"), it.get("answer"))
        elif t == "howto":
            add(b.get("name"))
            for st in b.get("steps", []):
                add(st.get("name"), st.get("text"))
        elif t == "features":
            add(b.get("heading"))
            for it in b.get("items", []):
                add(it.get("title"), it.get("text"))
        elif t == "table":
            add(" ".join(b.get("headers", [])), *(" ".join(r) for r in b.get("rows", [])))
        elif t == "cta":
            add(b.get("heading"), b.get("text"))
        elif t == "testimonial":
            add(b.get("quote"))
        elif t == "comparison":
            add(
                *(r["feature"] + " " + " ".join(r["values"]) for r in b.get("rows", [])),
                b.get("verdict"),
            )
        elif t == "image":
            add(b.get("caption"))
        elif t == "pricing":
            for p in b.get("plans", []):
                add(p.get("name"), p.get("description"), *p.get("features", []))
    text = "\n".join(out)
    return re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text).replace("**", "").replace("`", "")
