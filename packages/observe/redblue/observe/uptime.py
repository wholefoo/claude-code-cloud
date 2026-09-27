"""Synthetic uptime checks against the site's own key pages.

Checks are same-origin only: callers configure *paths* (``/pricing``), never URLs, and
requests always go to the site's own base URL (or a supplied client bound to it).
"""

from __future__ import annotations

import time
from collections.abc import Iterable
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel

from redblue.core.db import utcnow
from redblue.observe.models import UptimeCheck
from redblue.observe.store import DB, use_session

DEFAULT_PATHS = ("/", "/_rb/health")
USER_AGENT = "RedBlue-Uptime/1.0"


class UptimeResult(BaseModel):
    path: str
    status: int
    duration_ms: float
    ok: bool
    error: str = ""


def validate_path(path: str) -> str:
    """Return ``path`` if it is a same-origin absolute path; raise ValueError otherwise."""
    if not isinstance(path, str) or not path:
        raise ValueError("uptime path must be a non-empty string")
    if any(ord(c) < 0x20 or ord(c) == 0x7F for c in path) or "\\" in path:
        raise ValueError(f"uptime path contains forbidden characters: {path!r}")
    if not path.startswith("/") or path.startswith("//"):
        raise ValueError(f"uptime checks accept site paths like '/pricing', not URLs: {path!r}")
    parts = urlsplit(path)
    if parts.scheme or parts.netloc:
        raise ValueError(f"uptime checks accept site paths, not URLs: {path!r}")
    return path


def run_checks(
    db: DB,
    paths: Iterable[str] = DEFAULT_PATHS,
    *,
    client: httpx.Client | None = None,
    base_url: str | None = None,
    timeout: float = 10.0,
    slow_ms: float | None = None,
) -> list[UptimeResult]:
    """GET each path, record an :class:`UptimeCheck` per path, and return the results.

    Pass either an ``httpx.Client`` already bound to the site (e.g. a ``TestClient``) or
    the site's ``base_url`` (from settings). A check is ok for a 2xx/3xx response
    (redirects are not followed) that finishes within ``slow_ms`` if given.
    """
    checked = [validate_path(p) for p in paths]  # validate all before sending any
    if client is None and not base_url:
        raise ValueError("run_checks needs a client or the site's base_url")
    own = client is None
    http = client or httpx.Client(base_url=base_url, timeout=timeout, follow_redirects=False)
    results: list[UptimeResult] = []
    try:
        for path in checked:
            started = time.perf_counter()
            try:
                resp = http.get(path, headers={"User-Agent": USER_AGENT}, follow_redirects=False)
                ms = (time.perf_counter() - started) * 1000
                ok = 200 <= resp.status_code < 400
                error = "" if ok else f"HTTP {resp.status_code}"
                if ok and slow_ms is not None and ms > slow_ms:
                    ok, error = False, f"slow: {ms:.0f}ms > {slow_ms:.0f}ms"
                results.append(
                    UptimeResult(
                        path=path, status=resp.status_code, duration_ms=ms, ok=ok, error=error
                    )
                )
            except Exception as exc:  # network errors, timeouts
                ms = (time.perf_counter() - started) * 1000
                results.append(
                    UptimeResult(
                        path=path,
                        status=0,
                        duration_ms=ms,
                        ok=False,
                        error=f"{type(exc).__name__}: {exc}"[:500],
                    )
                )
    finally:
        if own:
            http.close()
    now = utcnow()
    with use_session(db) as s:
        for r in results:
            s.add(
                UptimeCheck(
                    path=r.path,
                    ts=now,
                    status=r.status,
                    duration_ms=round(r.duration_ms, 3),
                    ok=r.ok,
                    error=r.error,
                )
            )
    return results
