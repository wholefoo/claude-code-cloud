"""Typed pipeline artifacts. Everything a model produces is validated against these."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field, field_validator, model_validator

from redblue.core.security import is_safe_link


class TrendSignal(BaseModel):
    source: str  # youtube / tavily / reddit / manual
    title: str
    url: str | None = None
    topic: str  # normalised topic phrase used for research
    published_at: datetime | None = None
    views: int | None = None
    likes: int | None = None
    comments: int | None = None
    snippet: str = ""
    extra: dict = Field(default_factory=dict)


class Source(BaseModel):
    title: str
    url: str
    content: str = ""  # extracted text (untrusted)

    @field_validator("url")
    @classmethod
    def _url(cls, v: str) -> str:
        if not v.startswith(("http://", "https://")) or not is_safe_link(v):
            raise ValueError("Source URLs must be http(s).")
        return v


class Fact(BaseModel):
    claim: str = Field(max_length=400)
    source: int = Field(ge=0, description="Index into Brief.sources")


class Brief(BaseModel):
    topic: str
    angle: str = Field(max_length=300)
    audience: str = Field(max_length=200)
    hooks: list[str] = Field(min_length=1, max_length=5)
    facts: list[Fact] = Field(default_factory=list, max_length=15)
    gaps: str = Field(default="", max_length=500, description="What existing videos miss")
    sources: list[Source] = Field(default_factory=list)

    @model_validator(mode="after")
    def _facts_cite_sources(self) -> Brief:
        for f in self.facts:
            if f.source >= len(self.sources):
                raise ValueError(f"Fact cites missing source #{f.source}: {f.claim[:60]}")
        return self


class Beat(BaseModel):
    narration: str = Field(min_length=1, max_length=400)
    on_screen_text: str = Field(default="", max_length=80)
    visual_query: str = Field(max_length=80, description="Stock-footage search terms")
    seconds: float = Field(ge=1.5, le=15)
    sources: list[int] = Field(default_factory=list, description="Brief source indices")


class Script(BaseModel):
    title: str = Field(max_length=100)
    hook: str = Field(max_length=200)
    beats: list[Beat] = Field(min_length=2, max_length=14)
    cta: str = Field(max_length=150)
    hashtags: list[str] = Field(default_factory=list, max_length=8)

    @property
    def duration(self) -> float:
        return sum(b.seconds for b in self.beats)

    def validate_against(self, brief: Brief, max_seconds: float = 90) -> list[str]:
        """Problems that must be fixed before rendering."""
        problems = []
        for i, b in enumerate(self.beats):
            bad = [s for s in b.sources if s >= len(brief.sources)]
            if bad:
                problems.append(f"Beat {i + 1} cites missing source(s) {bad}.")
            if any(ch.isdigit() for ch in b.narration) and not b.sources:
                problems.append(f"Beat {i + 1} states a number without citing a source.")
        if self.duration > max_seconds:
            problems.append(f"Script runs {self.duration:.0f}s; keep it under {max_seconds}s.")
        return problems


class Asset(BaseModel):
    kind: str  # video / audio
    path: str
    provider: str  # pexels / elevenlabs / generated
    license: str
    attribution: str = ""
    source_url: str | None = None
