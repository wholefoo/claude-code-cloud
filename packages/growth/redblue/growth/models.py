"""Growth tables: cookieless analytics, subscribers with consent records, leads,
experiments, banners, Search Console imports, and drafted (never auto-sent) social posts."""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import JSON, Boolean, Date, DateTime, Float, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from redblue.core.db import Base, utcnow


class DailySalt(Base):
    """Rotated daily and deleted after 2 days, so visitor hashes can't be linked over time."""

    __tablename__ = "rb_growth_salts"

    day: Mapped[date] = mapped_column(Date, primary_key=True)
    salt: Mapped[str] = mapped_column(String(64))


class PageView(Base):
    __tablename__ = "rb_growth_pageviews"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    day: Mapped[date] = mapped_column(Date, index=True)
    path: Mapped[str] = mapped_column(String(500), index=True)
    visitor: Mapped[str] = mapped_column(String(32), index=True)
    source: Mapped[str] = mapped_column(String(20), index=True)  # search/social/ai/email/...
    referrer_host: Mapped[str] = mapped_column(String(255), default="")
    utm_source: Mapped[str] = mapped_column(String(100), default="")
    utm_medium: Mapped[str] = mapped_column(String(100), default="")
    utm_campaign: Mapped[str] = mapped_column(String(200), default="")
    ai_assistant: Mapped[str] = mapped_column(String(50), default="")
    device: Mapped[str] = mapped_column(String(10), default="desktop")
    variants: Mapped[dict] = mapped_column(JSON, default=dict)


class GoalEvent(Base):
    __tablename__ = "rb_growth_goals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    day: Mapped[date] = mapped_column(Date, index=True)
    goal: Mapped[str] = mapped_column(String(100), index=True)
    path: Mapped[str] = mapped_column(String(500))
    visitor: Mapped[str] = mapped_column(String(32), index=True)
    variants: Mapped[dict] = mapped_column(JSON, default=dict)


class Subscriber(Base):
    __tablename__ = "rb_growth_subscribers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    status: Mapped[str] = mapped_column(String(20), default="pending")  # confirmed/unsubscribed
    list_name: Mapped[str] = mapped_column(String(50), default="newsletter")
    consent_text: Mapped[str] = mapped_column(Text)
    consent_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    consent_source: Mapped[str] = mapped_column(String(500), default="")
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    unsubscribed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    lead_magnet: Mapped[str | None] = mapped_column(String(200), nullable=True)


class Lead(Base):
    __tablename__ = "rb_growth_leads"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    kind: Mapped[str] = mapped_column(String(30))
    name: Mapped[str] = mapped_column(String(200), default="")
    email: Mapped[str] = mapped_column(String(320))
    message: Mapped[str] = mapped_column(Text, default="")
    source_path: Mapped[str] = mapped_column(String(500), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    handled: Mapped[bool] = mapped_column(Boolean, default=False)


class Experiment(Base):
    __tablename__ = "rb_growth_experiments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    key: Mapped[str] = mapped_column(String(80), unique=True)
    name: Mapped[str] = mapped_column(String(200))
    path: Mapped[str | None] = mapped_column(String(500), nullable=True)  # None = site-wide
    variants: Mapped[list] = mapped_column(JSON)  # [{"key": "a", "value": "...", "weight": 1}]
    goal: Mapped[str] = mapped_column(String(100))
    status: Mapped[str] = mapped_column(String(20), default="draft")  # running/stopped
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Banner(Base):
    __tablename__ = "rb_growth_banners"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    message: Mapped[str] = mapped_column(String(300))
    cta_label: Mapped[str] = mapped_column(String(60), default="")
    cta_url: Mapped[str] = mapped_column(String(500), default="")
    path_prefix: Mapped[str] = mapped_column(String(200), default="/")
    frequency_days: Mapped[int] = mapped_column(Integer, default=7)
    active: Mapped[bool] = mapped_column(Boolean, default=False)
    starts_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    ends_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class SearchConsoleRow(Base):
    __tablename__ = "rb_growth_search_console"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    day: Mapped[date] = mapped_column(Date, index=True)
    query: Mapped[str] = mapped_column(String(500), index=True)
    page: Mapped[str] = mapped_column(String(500), index=True)
    clicks: Mapped[int] = mapped_column(Integer, default=0)
    impressions: Mapped[int] = mapped_column(Integer, default=0)
    position: Mapped[float] = mapped_column(Float, default=0.0)


class SocialDraft(Base):
    """Drafted by the repurposing agent. There is no code path that posts these."""

    __tablename__ = "rb_growth_social_drafts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    entry_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    channel: Mapped[str] = mapped_column(String(30))  # linkedin/x/mastodon/email/summary
    text: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), default="draft")  # draft/approved/archived
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class GrowthReportRecord(Base):
    __tablename__ = "rb_growth_reports"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    report: Mapped[dict] = mapped_column(JSON)
