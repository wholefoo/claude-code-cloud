"""Read-side queries for the dashboard and the ops agent.

Percentiles are computed in Python so the same code runs on SQLite and Postgres.
"""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Sequence
from datetime import datetime, timedelta

from sqlalchemy import func, select

from redblue.core.ai import AgentCall
from redblue.core.db import utcnow
from redblue.observe.models import Deploy, ErrorGroup, RequestMetric, UptimeCheck
from redblue.observe.store import DB, use_session

DEFAULT_WINDOW = timedelta(hours=24)


def percentile(values: Sequence[float], q: float) -> float | None:
    """Linear-interpolated percentile (same as numpy's default). ``q`` in [0, 100]."""
    if not values:
        return None
    if not 0 <= q <= 100:
        raise ValueError("q must be between 0 and 100")
    data = sorted(values)
    if len(data) == 1:
        return float(data[0])
    k = (len(data) - 1) * q / 100
    lo, hi = math.floor(k), math.ceil(k)
    if lo == hi:
        return float(data[lo])
    return float(data[lo] + (data[hi] - data[lo]) * (k - lo))


def _summary(durations: list[float], statuses: list[int]) -> dict:
    n = len(durations)
    errors = sum(1 for s in statuses if s >= 500)
    return {
        "count": n,
        "errors": errors,
        "error_rate": round(errors / n, 4) if n else 0.0,
        "p50_ms": _r(percentile(durations, 50)),
        "p95_ms": _r(percentile(durations, 95)),
        "p99_ms": _r(percentile(durations, 99)),
        "mean_ms": _r(sum(durations) / n) if n else None,
    }


def _r(v: float | None) -> float | None:
    return None if v is None else round(v, 2)


def _window(
    since: datetime | None, until: datetime | None, window: timedelta
) -> tuple[datetime, datetime]:
    until = until or utcnow()
    return since or until - window, until


def route_stats(
    db: DB,
    *,
    since: datetime | None = None,
    until: datetime | None = None,
    window: timedelta = DEFAULT_WINDOW,
) -> list[dict]:
    """Per route: method, route, count, errors, error_rate, p50/p95/p99/mean (ms).

    Sorted by request count, busiest first.
    """
    start, end = _window(since, until, window)
    groups: dict[tuple[str, str], tuple[list[float], list[int]]] = defaultdict(lambda: ([], []))
    with use_session(db) as s:
        rows = s.execute(
            select(
                RequestMetric.method,
                RequestMetric.route,
                RequestMetric.duration_ms,
                RequestMetric.status,
            ).where(RequestMetric.ts >= start, RequestMetric.ts < end)
        ).all()
    for method, route, dur, status in rows:
        d, st = groups[(method, route)]
        d.append(dur)
        st.append(status)
    out = [{"method": m, "route": r, **_summary(d, st)} for (m, r), (d, st) in groups.items()]
    out.sort(key=lambda x: (-x["count"], x["route"], x["method"]))
    return out


def timeseries(
    db: DB,
    *,
    since: datetime | None = None,
    until: datetime | None = None,
    window: timedelta = DEFAULT_WINDOW,
    bucket: timedelta = timedelta(hours=1),
    route: str | None = None,
    method: str | None = None,
) -> list[dict]:
    """Fixed-width buckets (oldest first): ts, count, errors, error_rate, p50/p95/p99."""
    start, end = _window(since, until, window)
    size = bucket.total_seconds()
    if size <= 0:
        raise ValueError("bucket must be positive")
    n = max(1, math.ceil((end - start).total_seconds() / size))
    if n > 2000:
        raise ValueError("too many buckets; use a larger bucket size")
    q = select(RequestMetric.ts, RequestMetric.duration_ms, RequestMetric.status).where(
        RequestMetric.ts >= start, RequestMetric.ts < end
    )
    if route is not None:
        q = q.where(RequestMetric.route == route)
    if method is not None:
        q = q.where(RequestMetric.method == method.upper())
    buckets: list[tuple[list[float], list[int]]] = [([], []) for _ in range(n)]
    with use_session(db) as s:
        for ts, dur, status in s.execute(q).all():
            i = min(n - 1, int((ts - start).total_seconds() // size))
            buckets[i][0].append(dur)
            buckets[i][1].append(status)
    return [
        {"ts": (start + bucket * i).isoformat(), **_summary(d, st)}
        for i, (d, st) in enumerate(buckets)
    ]


def compare_windows(
    db: DB,
    before: tuple[datetime, datetime],
    after: tuple[datetime, datetime],
) -> list[dict]:
    """Per route: ``before`` and ``after`` summaries plus p95 ratio and error-rate delta."""
    b = {(r["method"], r["route"]): r for r in route_stats(db, since=before[0], until=before[1])}
    a = {(r["method"], r["route"]): r for r in route_stats(db, since=after[0], until=after[1])}
    out = []
    for key in sorted(set(a) | set(b), key=lambda k: (k[1], k[0])):
        rb, ra = b.get(key), a.get(key)
        ratio = None
        if rb and ra and rb["p95_ms"] and ra["p95_ms"] is not None:
            ratio = round(ra["p95_ms"] / rb["p95_ms"], 2)
        out.append(
            {
                "method": key[0],
                "route": key[1],
                "before": rb,
                "after": ra,
                "p95_ratio": ratio,
                "error_rate_delta": round(
                    (ra["error_rate"] if ra else 0.0) - (rb["error_rate"] if rb else 0.0), 4
                ),
            }
        )
    return out


def compare_deploy(db: DB, deploy_id: int, window: timedelta = DEFAULT_WINDOW) -> dict:
    """Compare ``window`` before a deploy marker with ``window`` after it (or until now)."""
    with use_session(db) as s:
        dep = s.get(Deploy, deploy_id)
        if dep is None:
            raise LookupError(f"deploy {deploy_id} not found")
        at = dep.created_at
        info = deploy_dict(dep)
    end = min(utcnow(), at + window)
    return {"deploy": info, "routes": compare_windows(db, (at - window, at), (at, end))}


def deploy_dict(d: Deploy) -> dict:
    return {
        "id": d.id,
        "version": d.version,
        "note": d.note,
        "created_at": d.created_at.isoformat(),
    }


def list_deploys(db: DB, limit: int = 20) -> list[dict]:
    with use_session(db) as s:
        rows = s.scalars(
            select(Deploy).order_by(Deploy.created_at.desc(), Deploy.id.desc()).limit(limit)
        ).all()
        return [deploy_dict(d) for d in rows]


def error_dict(g: ErrorGroup, *, traceback: bool = False) -> dict:
    out = {
        "id": g.id,
        "fingerprint": g.fingerprint,
        "exc_type": g.exc_type,
        "message": g.message,
        "top_frame": g.top_frame,
        "first_seen": g.first_seen.isoformat(),
        "last_seen": g.last_seen.isoformat(),
        "count": g.count,
        "status": g.status,
        "sample_request_id": g.sample_request_id,
    }
    if traceback:
        out["sample_traceback"] = g.sample_traceback
    return out


def top_errors(
    db: DB, *, limit: int = 20, status: str | None = "open", since: datetime | None = None
) -> list[dict]:
    """Error groups, most frequent first (``status=None`` for all)."""
    q = select(ErrorGroup)
    if status:
        q = q.where(ErrorGroup.status == status)
    if since:
        q = q.where(ErrorGroup.last_seen >= since)
    q = q.order_by(ErrorGroup.count.desc(), ErrorGroup.last_seen.desc()).limit(limit)
    with use_session(db) as s:
        return [error_dict(g) for g in s.scalars(q).all()]


def get_error(db: DB, group_id: int) -> dict | None:
    with use_session(db) as s:
        g = s.get(ErrorGroup, group_id)
        return error_dict(g, traceback=True) if g else None


def agent_costs(db: DB, *, days: int = 30, ai=None) -> dict:
    """Tokens and dollars per agent, per task and per page, plus totals.

    ``by_agent`` comes from :meth:`AIClient.cost_summary` when an ``ai`` client is given.
    """
    since = utcnow() - timedelta(days=days)

    def grouped(col) -> list[dict]:
        rows = s.execute(
            select(
                col,
                func.count(AgentCall.id),
                func.sum(AgentCall.input_tokens),
                func.sum(AgentCall.output_tokens),
                func.sum(AgentCall.cost_usd),
            )
            .where(AgentCall.created_at >= since)
            .group_by(col)
        ).all()
        out = [
            {
                "key": k or "",
                "calls": c,
                "input_tokens": int(i or 0),
                "output_tokens": int(o or 0),
                "cost_usd": round(float(cost or 0), 4),
            }
            for k, c, i, o, cost in rows
        ]
        out.sort(key=lambda r: (-r["cost_usd"], r["key"]))
        return out

    with use_session(db) as s:
        by_task = grouped(AgentCall.task)
        by_page = [r for r in grouped(AgentCall.page) if r["key"]]
        if ai is not None:
            by_agent = sorted(ai.cost_summary(days), key=lambda r: -r["cost_usd"])
        else:
            by_agent = [
                {**{k: v for k, v in r.items() if k != "key"}, "agent": r["key"]}
                for r in grouped(AgentCall.agent)
            ]
    return {
        "days": days,
        "total_cost_usd": round(sum(r["cost_usd"] for r in by_agent), 4),
        "total_calls": sum(r["calls"] for r in by_agent),
        "by_agent": by_agent,
        "by_task": by_task,
        "by_page": by_page,
    }


def uptime_summary(db: DB, *, window: timedelta = DEFAULT_WINDOW) -> list[dict]:
    """Per checked path: checks, failures, availability, p95 latency, last result."""
    since = utcnow() - window
    with use_session(db) as s:
        rows = s.scalars(
            select(UptimeCheck).where(UptimeCheck.ts >= since).order_by(UptimeCheck.ts)
        ).all()
        by_path: dict[str, list[UptimeCheck]] = defaultdict(list)
        for r in rows:
            by_path[r.path].append(r)
        out = []
        for path, checks in sorted(by_path.items()):
            ok = sum(1 for c in checks if c.ok)
            last = checks[-1]
            out.append(
                {
                    "path": path,
                    "checks": len(checks),
                    "failures": len(checks) - ok,
                    "availability": round(ok / len(checks), 4),
                    "p95_ms": _r(percentile([c.duration_ms for c in checks], 95)),
                    "last_ok": last.ok,
                    "last_status": last.status,
                    "last_error": last.error,
                    "last_ts": last.ts.isoformat(),
                }
            )
    return out
