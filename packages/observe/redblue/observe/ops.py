"""The ops agent: finds anomalies, explains them in plain language, drafts fix tasks.

Detection and explanations are deterministic and work without an API key. With a key,
explanations and fix drafts come from the model, validated against Pydantic schemas,
with all metrics/log evidence fenced via :func:`redblue.core.ai.untrusted`.

The ops agent never changes code or deploys. :meth:`OpsAgent.propose_fix` returns a
:class:`BuilderTask` draft that the builder picks up; its PR then goes through the gate
and a human merges it.
"""

from __future__ import annotations

import json
import logging
from collections import defaultdict
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, Field
from sqlalchemy import func, select

from redblue.core.ai import untrusted
from redblue.core.db import utcnow
from redblue.observe.metrics import compare_windows
from redblue.observe.models import Deploy, ErrorEvent, ErrorGroup, UptimeCheck
from redblue.observe.store import DB, use_session

if TYPE_CHECKING:
    from redblue.core.context import Platform

log = logging.getLogger("redblue.observe.ops")

AnomalyKind = Literal["latency_regression", "error_spike", "new_error", "uptime_failure"]
Severity = Literal["low", "medium", "high"]
_SEVERITY_ORDER = {"high": 0, "medium": 1, "low": 2}


class DeployRef(BaseModel):
    id: int
    version: str
    created_at: datetime
    note: str = ""


class Anomaly(BaseModel):
    kind: AnomalyKind
    route: str  # route template ("/pricing") or uptime path
    method: str = ""
    severity: Severity
    metric: str  # p95_ms | error_rate | count | availability
    metric_before: float | None = None
    metric_after: float | None = None
    deploy: DeployRef | None = None
    evidence: dict[str, Any] = Field(default_factory=dict)

    @property
    def label(self) -> str:
        return f"{self.method} {self.route}".strip()

    @property
    def ratio(self) -> float | None:
        if self.metric_before and self.metric_after is not None:
            return self.metric_after / self.metric_before
        return None


class OpsExplanation(BaseModel):
    summary: str = Field(max_length=2000)
    likely_cause: str = Field(max_length=2000)
    suggested_fix: str = Field(max_length=4000)
    confidence: float = Field(ge=0.0, le=1.0)


class BuilderTask(BaseModel):
    """A draft task for the builder agent. Drafts only: nothing is applied or deployed."""

    title: str = Field(max_length=200)
    description: str = Field(max_length=8000)
    affected_route: str = Field(max_length=300)
    acceptance_criteria: list[str] = Field(default_factory=list, max_length=20)
    source: Literal["ops"] = "ops"


# ---------------------------------------------------------------- plain-language text


def _ms(v: float | None) -> str:
    return "n/a" if v is None else f"{v:.0f}ms"


def _pct(v: float | None) -> str:
    return "n/a" if v is None else f"{v * 100:.1f}%"


def _deploy_phrase(a: Anomaly) -> str:
    if a.deploy is None:
        return f"compared with the previous {a.evidence.get('window_hours', 24):g}h"
    d = a.deploy
    return f"after deploy {d.version} on {d.created_at:%A} ({d.created_at:%Y-%m-%d %H:%M} UTC)"


def describe(a: Anomaly) -> str:
    """Deterministic one-paragraph explanation; always available."""
    when = _deploy_phrase(a)
    ev = a.evidence
    if a.kind == "latency_regression":
        ratio = a.ratio or 0.0
        return (
            f"p95 of {a.label} went {_ms(a.metric_before)} → {_ms(a.metric_after)} "
            f"({ratio:.1f}x) {when}, over {ev.get('count_after', '?')} requests "
            f"(p50 {_ms(ev.get('p50_before'))} → {_ms(ev.get('p50_after'))})."
        )
    if a.kind == "error_spike":
        return (
            f"Error rate of {a.label} rose from {_pct(a.metric_before)} to "
            f"{_pct(a.metric_after)} {when} ({ev.get('errors_after', '?')} of "
            f"{ev.get('count_after', '?')} requests returned 5xx)."
        )
    if a.kind == "new_error":
        where = f" in {a.label}" if a.route else ""
        return (
            f"New error {ev.get('exc_type', 'exception')}{where}: "
            f'"{str(ev.get("message", ""))[:160]}" at {ev.get("top_frame") or "unknown frame"}, '
            f"seen {int(a.metric_after or 0)} times since it first appeared {when}."
        )
    # uptime_failure
    return (
        f"Uptime check for {a.route} failed {ev.get('failures', '?')} of "
        f"{ev.get('checks', '?')} times (availability {_pct(a.metric_after)}, was "
        f"{_pct(a.metric_before)}) {when}; last error: {ev.get('last_error') or 'n/a'}."
    )


def _fallback_explanation(a: Anomaly) -> OpsExplanation:
    deploy = f"deploy {a.deploy.version}" if a.deploy else "a recent change"
    causes = {
        "latency_regression": (
            f"A change in {deploy} likely added work to {a.label}: a new or unindexed "
            "query, an N+1 pattern, a missing cache, or a slow external call.",
            f"Profile {a.label} (compare DB query counts and timings before and after "
            f"{deploy}), add the missing index or eager loading, or cache the result.",
        ),
        "error_spike": (
            f"{a.label} started failing after {deploy}; a code path now raises for some inputs "
            "or a dependency (DB, storage, external API) is failing.",
            f"Check the top error groups for {a.label}, reproduce with a test, and fix the "
            "failing path; roll back if the impact is large.",
        ),
        "new_error": (
            f"A code path introduced or exposed by {deploy} raises "
            f"{a.evidence.get('exc_type', 'an exception')}.",
            "Write a failing test from the sample traceback, then handle the bad input or "
            "state at the top in-app frame.",
        ),
        "uptime_failure": (
            f"{a.route} is unreachable or returning errors; the app, its database, or the "
            "reverse proxy may be down or misconfigured.",
            f"Open {a.route} manually, check the application logs and error groups for the "
            "same period, and restore the failing dependency.",
        ),
    }
    cause, fix = causes[a.kind]
    return OpsExplanation(
        summary=describe(a),
        likely_cause=cause,
        suggested_fix=fix,
        confidence=0.5 if a.deploy else 0.3,
    )


def _fallback_task(a: Anomaly, exp: OpsExplanation) -> BuilderTask:
    titles = {
        "latency_regression": f"Fix p95 latency regression on {a.label}",
        "error_spike": f"Fix 5xx error spike on {a.label}",
        "new_error": f"Fix {a.evidence.get('exc_type', 'new error')} on {a.label or 'job'}",
        "uptime_failure": f"Restore availability of {a.route}",
    }
    criteria = {
        "latency_regression": [
            f"p95 of {a.label} is back to within 20% of {_ms(a.metric_before)} in a local "
            "benchmark or the next deploy window.",
            "A test or query-count assertion guards against the regression.",
        ],
        "error_spike": [
            f"{a.label} returns no 5xx responses for the inputs that failed.",
            "A regression test reproduces the failure and passes after the fix.",
        ],
        "new_error": [
            "A regression test reproduces the exception and passes after the fix.",
            "The error group can be marked resolved and does not reopen.",
        ],
        "uptime_failure": [
            f"GET {a.route} returns 2xx/3xx.",
            "The uptime check passes for at least one full check interval.",
        ],
    }
    return BuilderTask(
        title=titles[a.kind][:200],
        description=(
            f"{exp.summary}\n\nLikely cause: {exp.likely_cause}\n\n"
            f"Suggested fix: {exp.suggested_fix}"
        )[:8000],
        affected_route=a.label[:300] or a.route[:300],
        acceptance_criteria=criteria[a.kind]
        + ["The change passes the RedBlue gate (tests, Red and Blue review) before merge."],
    )


# ---------------------------------------------------------------- the agent

EXPLAIN_SYSTEM = (
    "You are the RedBlue ops agent. You explain production anomalies (latency regressions, "
    "error spikes, new errors, failed uptime checks) of a small web site to its owner in "
    "plain language. Be concrete: name the route, the numbers and the deploy. Only state "
    "causes that the evidence supports; give a confidence between 0 and 1."
)
FIX_SYSTEM = (
    "You are the RedBlue ops agent drafting a task for the builder agent, which will open a "
    "pull request that goes through automated review and a human merge. Write a precise, "
    "testable task: title, description, affected route, and acceptance criteria. You never "
    "deploy or apply changes yourself."
)


class OpsAgent:
    def __init__(
        self,
        platform: Platform | None = None,
        *,
        latency_ratio: float = 1.5,
        min_latency_delta_ms: float = 50.0,
        min_samples: int = 5,
        error_rate_min: float = 0.05,
        error_ratio: float = 2.0,
    ):
        self.platform = platform
        self.latency_ratio = latency_ratio
        self.min_latency_delta_ms = min_latency_delta_ms
        self.min_samples = min_samples
        self.error_rate_min = error_rate_min
        self.error_ratio = error_ratio

    # -- helpers
    @property
    def ai(self):
        return self.platform.ai if self.platform is not None else None

    @property
    def ai_available(self) -> bool:
        return bool(self.ai is not None and self.ai.available)

    def _db(self, db: DB | None) -> DB:
        if db is not None:
            return db
        if self.platform is None:
            raise ValueError("OpsAgent needs a db or a platform")
        return self.platform.db

    # -- detection
    def detect_anomalies(
        self,
        db: DB | None = None,
        window: timedelta = timedelta(hours=24),
        *,
        now: datetime | None = None,
    ) -> list[Anomaly]:
        """Compare the latest ``window`` with the one before it.

        If a deploy marker falls inside the latest window, the split point moves to that
        deploy so "before" and "after" are measured around it, and anomalies name it.
        """
        db = self._db(db)
        now = now or utcnow()
        with use_session(db) as s:
            dep = s.scalar(
                select(Deploy)
                .where(Deploy.created_at > now - window, Deploy.created_at <= now)
                .order_by(Deploy.created_at.desc(), Deploy.id.desc())
                .limit(1)
            )
            deploy = (
                DeployRef(id=dep.id, version=dep.version, created_at=dep.created_at, note=dep.note)
                if dep
                else None
            )
        split = deploy.created_at if deploy else now - window
        before, after = (split - window, split), (split, now)
        base_ev = {
            "window_hours": window.total_seconds() / 3600,
            "before": [before[0].isoformat(), before[1].isoformat()],
            "after": [after[0].isoformat(), after[1].isoformat()],
        }
        out: list[Anomaly] = []
        out += self._route_anomalies(db, before, after, deploy, base_ev)
        out += self._new_errors(db, after, deploy, base_ev)
        out += self._uptime(db, before, after, deploy, base_ev)
        out.sort(key=lambda a: (_SEVERITY_ORDER[a.severity], a.kind, a.route))
        return out

    def _route_anomalies(self, db, before, after, deploy, base_ev) -> list[Anomaly]:
        out = []
        for row in compare_windows(db, before, after):
            b, a = row["before"], row["after"]
            if not b or not a or b["count"] < self.min_samples or a["count"] < self.min_samples:
                continue
            common = dict(route=row["route"], method=row["method"], deploy=deploy)
            ev = {
                **base_ev,
                "count_before": b["count"],
                "count_after": a["count"],
                "p50_before": b["p50_ms"],
                "p50_after": a["p50_ms"],
                "p99_before": b["p99_ms"],
                "p99_after": a["p99_ms"],
                "errors_before": b["errors"],
                "errors_after": a["errors"],
            }
            pb, pa = b["p95_ms"], a["p95_ms"]
            if (
                pb
                and pa is not None
                and pa >= pb * self.latency_ratio
                and pa - pb >= self.min_latency_delta_ms
            ):
                ratio = pa / pb
                sev: Severity = "high" if ratio >= 3 else "medium" if ratio >= 2 else "low"
                out.append(
                    Anomaly(
                        kind="latency_regression",
                        severity=sev,
                        metric="p95_ms",
                        metric_before=pb,
                        metric_after=pa,
                        evidence={**ev, "ratio": round(ratio, 2)},
                        **common,
                    )
                )
            eb, ea = b["error_rate"], a["error_rate"]
            if ea >= self.error_rate_min and ea >= max(eb * self.error_ratio, eb + 0.01):
                sev = "high" if ea >= 0.25 else "medium" if ea >= 0.1 else "low"
                out.append(
                    Anomaly(
                        kind="error_spike",
                        severity=sev,
                        metric="error_rate",
                        metric_before=eb,
                        metric_after=ea,
                        evidence=ev,
                        **common,
                    )
                )
        return out

    def _new_errors(self, db, after, deploy, base_ev) -> list[Anomaly]:
        out = []
        with use_session(db) as s:
            groups = s.scalars(
                select(ErrorGroup).where(
                    ErrorGroup.first_seen >= after[0],
                    ErrorGroup.first_seen <= after[1],
                    ErrorGroup.status == "open",
                )
            ).all()
            for g in groups:
                n = s.scalar(
                    select(func.count(ErrorEvent.id)).where(
                        ErrorEvent.group_id == g.id, ErrorEvent.ts >= after[0]
                    )
                )
                route = s.scalar(
                    select(ErrorEvent.route)
                    .where(ErrorEvent.group_id == g.id)
                    .order_by(ErrorEvent.ts.desc())
                    .limit(1)
                )
                method, _, path = (route or "").partition(" ")
                if not path:
                    method, path = "", method
                count = int(n or g.count)
                out.append(
                    Anomaly(
                        kind="new_error",
                        route=path,
                        method=method,
                        severity="high" if count >= 10 else "medium",
                        metric="count",
                        metric_before=0,
                        metric_after=count,
                        deploy=deploy,
                        evidence={
                            **base_ev,
                            "error_group_id": g.id,
                            "exc_type": g.exc_type,
                            "message": g.message[:300],
                            "top_frame": g.top_frame,
                            "first_seen": g.first_seen.isoformat(),
                            "traceback_tail": g.sample_traceback[-2000:],
                        },
                    )
                )
        return out

    def _uptime(self, db, before, after, deploy, base_ev) -> list[Anomaly]:
        with use_session(db) as s:
            rows = s.execute(
                select(UptimeCheck.path, UptimeCheck.ts, UptimeCheck.ok, UptimeCheck.error)
                .where(UptimeCheck.ts >= before[0], UptimeCheck.ts <= after[1])
                .order_by(UptimeCheck.ts)
            ).all()
        stats: dict[str, dict[str, list]] = defaultdict(lambda: {"before": [], "after": []})
        for path, ts, ok, error in rows:
            stats[path]["after" if ts >= after[0] else "before"].append((ok, error))
        out = []
        for path, st in sorted(stats.items()):
            aft = st["after"]
            fails = [e for ok, e in aft if not ok]
            if not fails:
                continue
            avail_a = (len(aft) - len(fails)) / len(aft)
            bef = st["before"]
            avail_b = sum(1 for ok, _ in bef if ok) / len(bef) if bef else None
            last_failed = not aft[-1][0]
            out.append(
                Anomaly(
                    kind="uptime_failure",
                    route=path,
                    method="GET",
                    severity="high" if last_failed or avail_a < 0.5 else "medium",
                    metric="availability",
                    metric_before=avail_b,
                    metric_after=round(avail_a, 4),
                    deploy=deploy,
                    evidence={
                        **base_ev,
                        "checks": len(aft),
                        "failures": len(fails),
                        "last_failed": last_failed,
                        "last_error": fails[-1],
                    },
                )
            )
        return out

    # -- explanation and fixes
    def explain(self, anomaly: Anomaly) -> OpsExplanation:
        """Plain-language explanation. Uses the model when available, else deterministic."""
        fallback = _fallback_explanation(anomaly)
        if not self.ai_available:
            return fallback
        try:
            return self.ai.structured(
                agent="ops",
                model=self.platform.settings.models.ops_triage,
                system=EXPLAIN_SYSTEM,
                prompt=self._prompt(anomaly, fallback),
                output=OpsExplanation,
                max_tokens=4000,
                task=f"explain:{anomaly.kind}",
                page=anomaly.route,
            )
        except Exception:
            log.warning("ops explain fell back to deterministic text", exc_info=True)
            return fallback

    def propose_fix(
        self, anomaly: Anomaly, explanation: OpsExplanation | None = None
    ) -> BuilderTask:
        """Draft a :class:`BuilderTask`. Never applies changes, opens PRs, or deploys."""
        exp = explanation or self.explain(anomaly)
        fallback = _fallback_task(anomaly, exp)
        if not self.ai_available:
            return fallback
        try:
            task = self.ai.structured(
                agent="ops",
                model=self.platform.settings.models.ops,
                system=FIX_SYSTEM,
                prompt=self._prompt(anomaly, exp),
                output=BuilderTask,
                max_tokens=8000,
                task=f"fix:{anomaly.kind}",
                page=anomaly.route,
            )
        except Exception:
            log.warning("ops propose_fix fell back to deterministic draft", exc_info=True)
            return fallback
        # The route is ours, not the model's: keep the task pinned to the anomaly.
        return task.model_copy(update={"affected_route": fallback.affected_route, "source": "ops"})

    @staticmethod
    def _prompt(anomaly: Anomaly, exp: OpsExplanation) -> str:
        # Everything derived from the anomaly (error messages, tracebacks, route names) can
        # carry attacker-controlled text, so all of it goes inside the untrusted fence.
        data = json.dumps(
            {
                "anomaly": anomaly.model_dump(mode="json"),
                "summary": describe(anomaly),
                "heuristic_cause": exp.likely_cause,
            },
            indent=2,
            default=str,
        )
        return (
            "An anomaly was detected by RedBlue's metrics. Its data follows as JSON; message "
            "and traceback fields come from application logs.\n"
            f"{untrusted('observability', data)}"
        )
