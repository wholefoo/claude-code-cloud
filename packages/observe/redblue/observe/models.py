"""Storage for the built-in lightweight observability store (tables prefixed ``rb_obs_``).

Routes are always stored as *templates* (``/posts/{slug}``), never raw paths, so the
tables stay small and do not collect identifiers or other personal data from URLs.
"""

from __future__ import annotations

from datetime import datetime

from redblue.core.db import Base, utcnow
from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

ERROR_STATUSES = ("open", "resolved", "ignored")


class Deploy(Base):
    """A deploy marker. Metrics recorded after it carry its id."""

    __tablename__ = "rb_obs_deploys"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    version: Mapped[str] = mapped_column(String(100))  # tag or git sha
    note: Mapped[str] = mapped_column(String(500), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


class RequestMetric(Base):
    __tablename__ = "rb_obs_requests"
    __table_args__ = (Index("ix_rb_obs_requests_route_ts", "route", "ts"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    method: Mapped[str] = mapped_column(String(10))
    route: Mapped[str] = mapped_column(String(300))  # template, never the raw path
    status: Mapped[int] = mapped_column(Integer)
    duration_ms: Mapped[float] = mapped_column(Float)
    request_id: Mapped[str] = mapped_column(String(128), default="")
    deploy_id: Mapped[int | None] = mapped_column(Integer, nullable=True)


class ErrorGroup(Base):
    __tablename__ = "rb_obs_error_groups"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    fingerprint: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    exc_type: Mapped[str] = mapped_column(String(200))
    message: Mapped[str] = mapped_column(String(1000), default="")
    top_frame: Mapped[str] = mapped_column(String(500), default="")
    first_seen: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_seen: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    count: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(20), default="open", index=True)
    sample_traceback: Mapped[str] = mapped_column(Text, default="")
    sample_request_id: Mapped[str] = mapped_column(String(128), default="")


class ErrorEvent(Base):
    __tablename__ = "rb_obs_error_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    group_id: Mapped[int] = mapped_column(
        ForeignKey("rb_obs_error_groups.id", ondelete="CASCADE"), index=True
    )
    ts: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    request_id: Mapped[str] = mapped_column(String(128), default="")
    route: Mapped[str] = mapped_column(String(300), default="")


class UptimeCheck(Base):
    __tablename__ = "rb_obs_uptime_checks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    path: Mapped[str] = mapped_column(String(300), index=True)
    ts: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    status: Mapped[int] = mapped_column(Integer, default=0)  # 0 = no response
    duration_ms: Mapped[float] = mapped_column(Float, default=0.0)
    ok: Mapped[bool] = mapped_column(Boolean, default=False)
    error: Mapped[str] = mapped_column(String(500), default="")
