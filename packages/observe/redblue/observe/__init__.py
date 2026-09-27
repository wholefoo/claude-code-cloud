"""redblue.observe: metrics, structured logs, error tracking, uptime checks and the ops agent.

Wire it into an app with :func:`install`; mount :func:`observe_router` in the admin.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import timedelta

from fastapi import FastAPI
from redblue.observe.dashboard import anomalies_view, dashboard_snapshot, observe_router
from redblue.observe.logging import configure_logging, get_request_id
from redblue.observe.middleware import ObservabilityMiddleware
from redblue.observe.ops import Anomaly, BuilderTask, OpsAgent, OpsExplanation, describe
from redblue.observe.otel import setup_otel, span
from redblue.observe.store import capture_exception, record_deploy
from redblue.observe.uptime import DEFAULT_PATHS, run_checks, validate_path

__all__ = [
    "Anomaly",
    "BuilderTask",
    "ObservabilityMiddleware",
    "OpsAgent",
    "OpsExplanation",
    "UPTIME_JOB",
    "anomalies_view",
    "capture_exception",
    "configure_logging",
    "dashboard_snapshot",
    "describe",
    "get_request_id",
    "install",
    "observe_router",
    "record_deploy",
    "run_checks",
    "setup_otel",
    "span",
]

UPTIME_JOB = "observe.uptime"


def install(
    app: FastAPI,
    *,
    sample_rate: float = 1.0,
    log_level: str = "INFO",
    json_logs: bool = True,
    configure_logs: bool = True,
    uptime_paths: Iterable[str] = DEFAULT_PATHS,
    uptime_interval: timedelta = timedelta(minutes=5),
    otel: bool = True,
) -> None:
    """Add observability to an app built by :func:`redblue.core.app.create_core_app`.

    Adds :class:`ObservabilityMiddleware` (outermost, so it sees every request and
    unhandled error), configures JSON logging, sets up OpenTelemetry when installed, and
    schedules uptime checks on ``app.state.rb.jobs`` if present.
    """
    rb = app.state.rb
    app.add_middleware(ObservabilityMiddleware, db=rb.db, sample_rate=sample_rate)
    if configure_logs:
        configure_logging(log_level, json=json_logs)
    otel_on = False
    if otel:
        otel_on = setup_otel(app, rb.settings.site_name, engine=rb.db.engine)

    paths = tuple(uptime_paths)
    for p in paths:
        validate_path(p)  # fail fast on misconfiguration (URLs are rejected)

    jobs = getattr(rb, "jobs", None)
    if jobs is not None and paths:

        def uptime_job(payload: dict) -> None:
            with span("observe.uptime", paths=len(paths)):
                run_checks(rb.db, paths, base_url=rb.settings.base_url)

        jobs.register(UPTIME_JOB)(uptime_job)
        if not any(name == UPTIME_JOB for name, _, _ in jobs.schedules):
            jobs.every(UPTIME_JOB, uptime_interval)

    rb.extras["observe"] = {
        "sample_rate": sample_rate,
        "uptime_paths": paths,
        "otel": otel_on,
    }
