"""JSON API for the admin's observability pages, plus template-friendly snapshot helpers.

Mounted by the admin under ``/admin/observe``. Reads need ``Role.editor``; resolving
errors and recording deploy markers need ``Role.admin``.
"""

from datetime import timedelta
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from redblue.core.auth import Role, User, require_role
from redblue.core.context import get_db
from redblue.observe import metrics
from redblue.observe.ops import OpsAgent, describe
from redblue.observe.store import DB, record_deploy, set_error_status


class DeployIn(BaseModel):
    version: str = Field(min_length=1, max_length=100)
    note: str = Field(default="", max_length=500)


class ErrorStatusIn(BaseModel):
    status: Literal["open", "resolved", "ignored"] = "resolved"


def anomalies_view(db: DB, *, window: timedelta = timedelta(hours=24), agent=None) -> list[dict]:
    """Anomalies as dicts with a deterministic ``explanation`` string for templates."""
    agent = agent or OpsAgent()
    return [
        {**a.model_dump(mode="json"), "label": a.label, "explanation": describe(a)}
        for a in agent.detect_anomalies(db, window)
    ]


def dashboard_snapshot(
    db: DB, *, window: timedelta = timedelta(hours=24), ai=None, cost_days: int = 30
) -> dict:
    """Everything the overview page needs, as plain dicts/lists."""
    routes = metrics.route_stats(db, window=window)
    total = sum(r["count"] for r in routes)
    errors = sum(r["errors"] for r in routes)
    bucket = timedelta(hours=1) if window <= timedelta(days=2) else timedelta(days=1)
    return {
        "window_hours": window.total_seconds() / 3600,
        "totals": {
            "requests": total,
            "errors": errors,
            "error_rate": round(errors / total, 4) if total else 0.0,
        },
        "routes": routes,
        "timeseries": metrics.timeseries(db, window=window, bucket=bucket),
        "errors": metrics.top_errors(db, limit=10),
        "anomalies": anomalies_view(db, window=window),
        "uptime": metrics.uptime_summary(db, window=window),
        "deploys": metrics.list_deploys(db, limit=10),
        "agent_costs": metrics.agent_costs(db, days=cost_days, ai=ai),
    }


def observe_router() -> APIRouter:
    router = APIRouter(tags=["observe"])
    Editor = Annotated[User, Depends(require_role(Role.editor))]
    Admin = Annotated[User, Depends(require_role(Role.admin))]
    Db = Annotated[Session, Depends(get_db)]
    Hours = Annotated[float, Query(gt=0, le=24 * 90)]

    @router.get("/metrics")
    def get_metrics(
        _user: Editor,
        db: Db,
        hours: Hours = 24,
        route: str | None = None,
        method: str | None = None,
        bucket_minutes: Annotated[int, Query(ge=1, le=60 * 24 * 7)] = 60,
    ) -> dict:
        window = timedelta(hours=hours)
        try:
            series = metrics.timeseries(
                db,
                window=window,
                bucket=timedelta(minutes=bucket_minutes),
                route=route,
                method=method,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {
            "window_hours": hours,
            "routes": metrics.route_stats(db, window=window),
            "timeseries": series,
        }

    @router.get("/errors")
    def get_errors(
        _user: Editor,
        db: Db,
        status: Literal["open", "resolved", "ignored", "all"] = "open",
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
    ) -> dict:
        wanted = None if status == "all" else status
        return {"errors": metrics.top_errors(db, limit=limit, status=wanted)}

    @router.get("/errors/{group_id}")
    def get_error(_user: Editor, db: Db, group_id: int) -> dict:
        err = metrics.get_error(db, group_id)
        if err is None:
            raise HTTPException(status_code=404, detail="Error group not found.")
        return err

    @router.post("/errors/{group_id}/resolve")
    def resolve_error(
        _user: Admin, db: Db, group_id: int, body: ErrorStatusIn | None = None
    ) -> dict:
        group = set_error_status(db, group_id, (body or ErrorStatusIn()).status)
        if group is None:
            raise HTTPException(status_code=404, detail="Error group not found.")
        return metrics.error_dict(group)

    @router.get("/anomalies")
    def get_anomalies(request: Request, _user: Editor, db: Db, hours: Hours = 24) -> dict:
        agent = OpsAgent(request.app.state.rb)
        return {"anomalies": anomalies_view(db, window=timedelta(hours=hours), agent=agent)}

    @router.get("/agent-costs")
    def get_agent_costs(
        request: Request,
        _user: Editor,
        db: Db,
        days: Annotated[int, Query(ge=1, le=365)] = 30,
    ) -> dict:
        return metrics.agent_costs(db, days=days, ai=request.app.state.rb.ai)

    @router.get("/deploys")
    def get_deploys(_user: Editor, db: Db) -> dict:
        return {"deploys": metrics.list_deploys(db, limit=50)}

    @router.post("/deploys", status_code=201)
    def post_deploy(_user: Admin, db: Db, body: DeployIn) -> dict:
        return metrics.deploy_dict(record_deploy(db, body.version, body.note))

    @router.get("/uptime")
    def get_uptime(_user: Editor, db: Db, hours: Hours = 24) -> dict:
        return {"uptime": metrics.uptime_summary(db, window=timedelta(hours=hours))}

    return router
