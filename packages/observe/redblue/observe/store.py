"""Writes to the observability store: deploy markers, request metrics, grouped errors."""

from __future__ import annotations

import hashlib
import os
import re
import sysconfig
import threading
import time
import traceback
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from types import TracebackType

from redblue.core.db import Database, utcnow
from redblue.observe.models import ERROR_STATUSES, Deploy, ErrorEvent, ErrorGroup, RequestMetric
from sqlalchemy import select
from sqlalchemy.orm import Session

DB = Database | Session


@contextmanager
def use_session(db: DB) -> Iterator[Session]:
    """Accept either a :class:`Database` (opens and commits a session) or a live Session."""
    if isinstance(db, Session):
        yield db
        db.flush()
    else:
        with db.session() as s:
            yield s


# ---------------------------------------------------------------- deploys

_deploy_cache: dict[int, tuple[float, int | None]] = {}
_deploy_lock = threading.Lock()
DEPLOY_CACHE_SECONDS = 30.0


def record_deploy(db: DB, version: str, note: str = "", at: datetime | None = None) -> Deploy:
    """Record a deploy marker (called by CI/the deploy script or the admin)."""
    version = version.strip()[:100]
    if not version:
        raise ValueError("A deploy marker needs a version or git sha.")
    with use_session(db) as s:
        d = Deploy(version=version, note=note[:500], created_at=at or utcnow())
        s.add(d)
        s.flush()
    with _deploy_lock:
        _deploy_cache.clear()
    return d


def current_deploy_id(db: Database) -> int | None:
    """Id of the most recent deploy marker, cached briefly to keep request writes cheap."""
    now = time.monotonic()
    key = id(db)
    with _deploy_lock:
        hit = _deploy_cache.get(key)
        if hit and now - hit[0] < DEPLOY_CACHE_SECONDS:
            return hit[1]
    with db.session() as s:
        dep_id = s.scalar(
            select(Deploy.id)
            .where(Deploy.created_at <= utcnow())
            .order_by(Deploy.created_at.desc(), Deploy.id.desc())
            .limit(1)
        )
    with _deploy_lock:
        _deploy_cache[key] = (now, dep_id)
    return dep_id


# ---------------------------------------------------------------- requests


def record_request(
    db: Database,
    *,
    method: str,
    route: str,
    status: int,
    duration_ms: float,
    request_id: str = "",
    ts: datetime | None = None,
) -> None:
    deploy_id = current_deploy_id(db)
    with db.session() as s:
        s.add(
            RequestMetric(
                ts=ts or utcnow(),
                method=method[:10],
                route=route[:300],
                status=status,
                duration_ms=round(duration_ms, 3),
                request_id=request_id[:128],
                deploy_id=deploy_id,
            )
        )


# ---------------------------------------------------------------- errors

_ADDR_RE = re.compile(r"0x[0-9a-fA-F]+")
_NOT_APP = tuple(
    p
    for p in {
        sysconfig.get_paths().get("stdlib"),
        sysconfig.get_paths().get("platstdlib"),
        sysconfig.get_paths().get("purelib"),
        sysconfig.get_paths().get("platlib"),
    }
    if p
)
_OWN_DIR = os.path.dirname(os.path.abspath(__file__))
MAX_FRAMES = 5


def _in_app(filename: str) -> bool:
    if filename.startswith("<"):
        return False
    path = os.path.abspath(filename)
    if path.startswith(_OWN_DIR + os.sep):
        return False  # our own middleware frames are noise
    if "site-packages" in path or "dist-packages" in path:
        return False
    return not path.startswith(_NOT_APP)


def _short_path(filename: str) -> str:
    path = os.path.abspath(filename)
    cwd = os.getcwd()
    if path.startswith(cwd + os.sep):
        return os.path.relpath(path, cwd)
    parts = path.split(os.sep)
    return os.sep.join(parts[-3:])


def normalize_message(message: str) -> str:
    return _ADDR_RE.sub("0x?", message)


def app_frames(tb: TracebackType | None) -> list[traceback.FrameSummary]:
    frames = traceback.extract_tb(tb)
    in_app = [f for f in frames if _in_app(f.filename)]
    return in_app or list(frames)


def fingerprint(exc: BaseException) -> str:
    """Stable id for "the same bug": exception type + innermost in-app frames.

    Line numbers, messages and memory addresses are ignored so the same failure with
    different values (ids, slugs, object reprs) or after an unrelated edit groups together.
    """
    frames = app_frames(exc.__traceback__)[-MAX_FRAMES:]
    etype = type(exc)
    parts = [f"{etype.__module__}.{etype.__qualname__}"]
    parts += [f"{_short_path(f.filename)}:{f.name}" for f in frames]
    return hashlib.sha256("|".join(parts).encode()).hexdigest()


def capture_exception(
    db: DB, exc: BaseException, *, request_id: str = "", route: str = ""
) -> ErrorGroup:
    """Group ``exc`` by fingerprint and record one event. Usable from jobs too."""
    fp = fingerprint(exc)
    frames = app_frames(exc.__traceback__)
    top = frames[-1] if frames else None
    top_frame = f"{_short_path(top.filename)}:{top.lineno} in {top.name}" if top else ""
    etype = type(exc)
    tb_text = "".join(traceback.format_exception(etype, exc, exc.__traceback__, limit=30))
    now = utcnow()
    with use_session(db) as s:
        group = s.scalar(select(ErrorGroup).where(ErrorGroup.fingerprint == fp))
        if group is None:
            group = ErrorGroup(
                fingerprint=fp,
                exc_type=f"{etype.__module__}.{etype.__qualname__}"[:200],
                first_seen=now,
                count=0,
                status="open",
            )
            s.add(group)
        elif group.status == "resolved":
            group.status = "open"  # regression: a resolved error came back
        group.message = normalize_message(str(exc))[:1000]
        group.top_frame = top_frame[:500]
        group.last_seen = now
        group.count = (group.count or 0) + 1
        group.sample_traceback = tb_text[-20000:]
        group.sample_request_id = request_id[:128]
        s.flush()
        s.add(ErrorEvent(group_id=group.id, ts=now, request_id=request_id[:128], route=route[:300]))
        s.flush()
    return group


def set_error_status(db: DB, group_id: int, status: str) -> ErrorGroup | None:
    if status not in ERROR_STATUSES:
        raise ValueError(f"status must be one of {ERROR_STATUSES}")
    with use_session(db) as s:
        group = s.get(ErrorGroup, group_id)
        if group is not None:
            group.status = status
    return group
