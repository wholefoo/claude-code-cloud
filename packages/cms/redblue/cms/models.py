"""CMS tables: entries (with live snapshot), revisions, media, redirects."""

from __future__ import annotations

import enum
from datetime import datetime

from redblue.core.db import Base, TimestampMixin, utcnow
from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column


class Status(enum.StrEnum):
    draft = "draft"
    in_review = "in_review"
    approved = "approved"
    scheduled = "scheduled"
    published = "published"
    archived = "archived"


class Entry(Base, TimestampMixin):
    """Working copy fields are edited; ``live`` holds the published snapshot the public site
    renders, so editing a published page never changes the site until it is re-published."""

    __tablename__ = "rb_cms_entries"
    __table_args__ = (UniqueConstraint("collection", "slug", "locale"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    collection: Mapped[str] = mapped_column(String(50), index=True)
    slug: Mapped[str] = mapped_column(String(200), index=True)
    locale: Mapped[str] = mapped_column(String(10), default="en")
    translation_of: Mapped[int | None] = mapped_column(
        ForeignKey("rb_cms_entries.id"), nullable=True
    )
    title: Mapped[str] = mapped_column(String(300))
    summary: Mapped[str] = mapped_column(Text, default="")
    blocks: Mapped[list] = mapped_column(JSON, default=list)
    data: Mapped[dict] = mapped_column(JSON, default=dict)  # collection-specific fields
    seo: Mapped[dict] = mapped_column(JSON, default=dict)
    tags: Mapped[list] = mapped_column(JSON, default=list)
    category: Mapped[str | None] = mapped_column(String(100), nullable=True)
    status: Mapped[Status] = mapped_column(Enum(Status), default=Status.draft, index=True)
    live: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    live_slug: Mapped[str | None] = mapped_column(String(200), nullable=True)
    author_id: Mapped[int | None] = mapped_column(ForeignKey("rb_users.id"), nullable=True)
    publish_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    content_updated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    ai_generated: Mapped[bool] = mapped_column(Boolean, default=False)
    ai_label: Mapped[bool] = mapped_column(Boolean, default=False)
    review_note: Mapped[str] = mapped_column(Text, default="")
    sort_order: Mapped[int] = mapped_column(Integer, default=0)

    @property
    def is_live(self) -> bool:
        return self.live is not None

    def snapshot(self) -> dict:
        return {
            "title": self.title,
            "slug": self.slug,
            "summary": self.summary,
            "blocks": self.blocks,
            "data": self.data,
            "seo": self.seo,
            "tags": self.tags,
            "category": self.category,
            "author_id": self.author_id,
            "ai_label": self.ai_label,
            "locale": self.locale,
        }


class Revision(Base):
    __tablename__ = "rb_cms_revisions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    entry_id: Mapped[int] = mapped_column(
        ForeignKey("rb_cms_entries.id", ondelete="CASCADE"), index=True
    )
    number: Mapped[int] = mapped_column(Integer)
    snapshot: Mapped[dict] = mapped_column(JSON)
    note: Mapped[str] = mapped_column(String(300), default="")
    author_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    by_agent: Mapped[str | None] = mapped_column(String(50), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Media(Base):
    __tablename__ = "rb_cms_media"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    key: Mapped[str] = mapped_column(String(200), unique=True)
    mime: Mapped[str] = mapped_column(String(100))
    size: Mapped[int] = mapped_column(Integer)
    sha256: Mapped[str] = mapped_column(String(64))
    alt: Mapped[str] = mapped_column(String(300))
    caption: Mapped[str] = mapped_column(String(500), default="")
    width: Mapped[int | None] = mapped_column(Integer, nullable=True)
    height: Mapped[int | None] = mapped_column(Integer, nullable=True)
    uploaded_by: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Redirect(Base):
    __tablename__ = "rb_cms_redirects"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    from_path: Mapped[str] = mapped_column(String(500), unique=True)
    to_path: Mapped[str] = mapped_column(String(500))
    status_code: Mapped[int] = mapped_column(Integer, default=301)
    automatic: Mapped[bool] = mapped_column(Boolean, default=False)
    hits: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
