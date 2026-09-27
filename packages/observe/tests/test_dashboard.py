from __future__ import annotations

import pytest
from sqlalchemy import select

from redblue.core.auth import Role, create_user
from redblue.observe import dashboard_snapshot, store
from redblue.observe.models import ErrorGroup

GETS = ["/metrics", "/errors", "/anomalies", "/agent-costs", "/deploys", "/uptime"]
P = "/admin/observe"


def _login(client, db, role: Role) -> None:
    with db.session() as s:
        uid = create_user(s, f"{role.name}@example.com", "correct horse battery", role).id
    assert client.get(f"/_test/login/{uid}").status_code == 200


def _csrf(client) -> dict:
    client.get("/")
    return {"x-csrf-token": client.cookies.get("rb_csrf")}


@pytest.mark.parametrize("path", GETS)
def test_requires_auth(client, path):
    assert client.get(P + path).status_code == 401


def test_viewer_is_forbidden(client, db):
    _login(client, db, Role.viewer)
    assert client.get(P + "/metrics").status_code == 403


def test_editor_can_read(client, db):
    client.get("/items/1")
    client.get("/boom/1")
    _login(client, db, Role.editor)
    for path in GETS:
        resp = client.get(P + path)
        assert resp.status_code == 200, path
    metrics = client.get(P + "/metrics", params={"hours": 1, "bucket_minutes": 5}).json()
    assert {r["route"] for r in metrics["routes"]} >= {"/items/{item_id}", "/boom/{n}"}
    assert len(metrics["timeseries"]) == 12
    errors = client.get(P + "/errors").json()["errors"]
    assert errors[0]["exc_type"] == "builtins.ValueError"
    detail = client.get(P + f"/errors/{errors[0]['id']}").json()
    assert "Traceback" in detail["sample_traceback"]
    assert client.get(P + "/errors/9999").status_code == 404
    anomalies = client.get(P + "/anomalies").json()["anomalies"]
    assert any(a["kind"] == "new_error" and "explanation" in a for a in anomalies)


def test_editor_cannot_resolve_or_deploy(client, db):
    client.get("/boom/1")
    _login(client, db, Role.editor)
    h = _csrf(client)
    assert client.post(P + "/errors/1/resolve", headers=h).status_code == 403
    assert client.post(P + "/deploys", json={"version": "x"}, headers=h).status_code == 403


def test_admin_resolve_and_deploy(client, db):
    client.get("/boom/1")
    _login(client, db, Role.admin)
    # CSRF is enforced on POSTs
    assert client.post(P + "/deploys", json={"version": "abc123"}).status_code == 403
    h = _csrf(client)
    with db.session() as s:
        gid = s.scalar(select(ErrorGroup.id))
    resp = client.post(P + f"/errors/{gid}/resolve", headers=h)
    assert resp.status_code == 200 and resp.json()["status"] == "resolved"
    resp = client.post(P + f"/errors/{gid}/resolve", json={"status": "ignored"}, headers=h)
    assert resp.json()["status"] == "ignored"
    assert client.post(P + "/errors/999/resolve", headers=h).status_code == 404
    resp = client.post(P + "/deploys", json={"version": "abc123", "note": "v1"}, headers=h)
    assert resp.status_code == 201 and resp.json()["version"] == "abc123"
    assert client.get(P + "/deploys").json()["deploys"][0]["version"] == "abc123"
    assert client.post(P + "/deploys", json={"version": ""}, headers=h).status_code == 422


def test_dashboard_snapshot(client, db):
    client.get("/")
    client.get("/boom/1")
    store.record_deploy(db, "sha1")
    snap = dashboard_snapshot(db)
    assert snap["totals"]["requests"] == 2 and snap["totals"]["errors"] == 1
    assert snap["errors"][0]["count"] == 1
    assert snap["deploys"][0]["version"] == "sha1"
    assert set(snap) >= {"routes", "timeseries", "anomalies", "uptime", "agent_costs"}
