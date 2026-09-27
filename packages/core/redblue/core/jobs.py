"""Postgres/SQLite-backed job queue: background tasks, scheduled jobs, retries.

No Redis required. Handlers are registered by name; payloads are JSON.
"""

from __future__ import annotations

import logging
import traceback
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

from redblue.core.db import Base, Database, utcnow
from sqlalchemy import JSON, DateTime, Integer, String, Text, select, update
from sqlalchemy.orm import Mapped, mapped_column

log = logging.getLogger("redblue.jobs")


class Job(Base):
    __tablename__ = "rb_jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(100), index=True)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(20), default="queued", index=True)
    run_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=5)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


Handler = Callable[[dict[str, Any]], None]


class JobQueue:
    def __init__(self, db: Database):
        self.db = db
        self.handlers: dict[str, Handler] = {}
        self.schedules: list[tuple[str, timedelta, dict]] = []

    def register(self, name: str) -> Callable[[Handler], Handler]:
        def deco(fn: Handler) -> Handler:
            self.handlers[name] = fn
            return fn

        return deco

    def every(self, name: str, interval: timedelta, payload: dict | None = None) -> None:
        """Schedule a recurring job (enqueued by :meth:`tick_schedules`)."""
        self.schedules.append((name, interval, payload or {}))

    def enqueue(
        self,
        name: str,
        payload: dict | None = None,
        run_at: datetime | None = None,
        max_attempts: int = 5,
    ) -> int:
        with self.db.session() as s:
            job = Job(
                name=name,
                payload=payload or {},
                run_at=run_at or utcnow(),
                max_attempts=max_attempts,
            )
            s.add(job)
            s.flush()
            return job.id

    def tick_schedules(self) -> None:
        with self.db.session() as s:
            for name, interval, payload in self.schedules:
                last = s.scalar(
                    select(Job.run_at).where(Job.name == name).order_by(Job.run_at.desc()).limit(1)
                )
                if last is None or last + interval <= utcnow():
                    s.add(Job(name=name, payload=payload))

    def _claim(self) -> Job | None:
        with self.db.session() as s:
            job = s.scalar(
                select(Job)
                .where(Job.status == "queued", Job.run_at <= utcnow())
                .order_by(Job.run_at)
                .limit(1)
                .with_for_update(skip_locked=True)
            )
            if job is None:
                return None
            # Optimistic claim works on SQLite (no SKIP LOCKED) as well.
            claimed = s.execute(
                update(Job)
                .where(Job.id == job.id, Job.status == "queued")
                .values(status="running", attempts=Job.attempts + 1)
            ).rowcount
            return job if claimed else None

    def run_once(self) -> bool:
        """Run one due job. Returns False if nothing was due."""
        job = self._claim()
        if job is None:
            return False
        handler = self.handlers.get(job.name)
        with self.db.session() as s:
            row = s.get(Job, job.id)
            try:
                if handler is None:
                    raise LookupError(f"No handler registered for job {job.name!r}")
                handler(dict(row.payload))
                row.status = "done"
                row.finished_at = utcnow()
            except Exception:
                row.last_error = traceback.format_exc(limit=5)
                if row.attempts >= row.max_attempts:
                    row.status = "failed"
                    log.error("job %s failed permanently", row.id)
                else:
                    row.status = "queued"
                    row.run_at = utcnow() + timedelta(seconds=min(3600, 2**row.attempts * 5))
        return True

    def drain(self, limit: int = 1000) -> int:
        n = 0
        while n < limit and self.run_once():
            n += 1
        return n
