"""Video tables: scored trends and video projects moving through the pipeline."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from redblue.core.db import Base, TimestampMixin, utcnow


class Trend(Base):
    __tablename__ = "rb_video_trends"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source: Mapped[str] = mapped_column(String(30))
    title: Mapped[str] = mapped_column(String(500))
    topic: Mapped[str] = mapped_column(String(300), index=True)
    url: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    signal: Mapped[dict] = mapped_column(JSON)
    score: Mapped[float] = mapped_column(Float, default=0.0, index=True)
    breakdown: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(20), default="new")  # new/picked/dismissed
    fetched_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


class VideoProject(Base, TimestampMixin):
    """status: brief → script → rendering → review → approved | rejected (| failed)."""

    __tablename__ = "rb_video_projects"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    trend_id: Mapped[int | None] = mapped_column(ForeignKey("rb_video_trends.id"), nullable=True)
    topic: Mapped[str] = mapped_column(String(300))
    status: Mapped[str] = mapped_column(String(20), default="brief", index=True)
    brief: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    script: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    assets: Mapped[list] = mapped_column(JSON, default=list)
    render_path: Mapped[str | None] = mapped_column(String(1000), nullable=True)  # first format
    template: Mapped[str | None] = mapped_column(String(40), nullable=True)  # None: setting
    formats: Mapped[list | None] = mapped_column(JSON, nullable=True)  # ["9:16", "16:9"]
    renders: Mapped[dict | None] = mapped_column(JSON, nullable=True)  # {"9:16": "/path.mp4"}
    problems: Mapped[list] = mapped_column(JSON, default=list)
    review_note: Mapped[str] = mapped_column(Text, default="")
    reviewed_by: Mapped[int | None] = mapped_column(Integer, nullable=True)
    ai_generated: Mapped[bool] = mapped_column(default=True)


class Publication(Base):
    """Where a human uploaded a rendered video. RedBlue never posts; people record it here
    so performance can be tracked and fed back into trend scoring."""

    __tablename__ = "rb_video_publications"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int] = mapped_column(
        ForeignKey("rb_video_projects.id", ondelete="CASCADE"), index=True
    )
    platform: Mapped[str] = mapped_column(String(20), index=True)  # youtube, tiktok, ...
    url: Mapped[str] = mapped_column(String(1000), unique=True)
    external_id: Mapped[str | None] = mapped_column(String(100), nullable=True)  # YouTube id
    format: Mapped[str | None] = mapped_column(String(10), nullable=True)  # "9:16"
    published_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    # Set only for direct uploads: who confirmed it, and the visibility they chose.
    uploaded_by: Mapped[str | None] = mapped_column(String(200), nullable=True)
    privacy: Mapped[str | None] = mapped_column(String(20), nullable=True)


class MetricSnapshot(Base):
    """Counts for a publication at one point in time (API fetch, CSV import or typed in)."""

    __tablename__ = "rb_video_metrics"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    publication_id: Mapped[int] = mapped_column(
        ForeignKey("rb_video_publications.id", ondelete="CASCADE"), index=True
    )
    taken_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    source: Mapped[str] = mapped_column(String(20))  # youtube_api / youtube_analytics / csv
    views: Mapped[int] = mapped_column(Integer, default=0)
    likes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    comments: Mapped[int | None] = mapped_column(Integer, nullable=True)
    shares: Mapped[int | None] = mapped_column(Integer, nullable=True)
    avg_view_pct: Mapped[float | None] = mapped_column(Float, nullable=True)  # retention 0–100
    avg_view_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
