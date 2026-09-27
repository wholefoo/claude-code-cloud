from __future__ import annotations

import io
import json
import logging

from fastapi.testclient import TestClient
from redblue.observe import store
from redblue.observe.logging import JSONFormatter, RequestIdFilter, request_id_var
from redblue.observe.middleware import accept_request_id
from redblue.observe.models import ErrorEvent, ErrorGroup, RequestMetric
from sqlalchemy import select


def _metrics(db) -> list[RequestMetric]:
    with db.session() as s:
        return list(s.scalars(select(RequestMetric).order_by(RequestMetric.id)))


def test_records_route_templates_not_raw_paths(client, db):
    assert client.get("/items/42").status_code == 200
    assert client.get("/items/7").status_code == 200
    assert client.get("/nope/secret-token").status_code == 404
    rows = _metrics(db)
    assert [(r.method, r.route, r.status) for r in rows] == [
        ("GET", "/items/{item_id}", 200),
        ("GET", "/items/{item_id}", 200),
        ("GET", "unmatched", 404),
    ]
    assert all(r.duration_ms >= 0 for r in rows)
    assert not any("42" in r.route or "secret" in r.route for r in rows)


def test_skips_static_media_and_beacon(client, db):
    client.get("/static/app.css")
    client.get("/media/x.png")
    client.post("/_rb/beacon", json={})
    assert _metrics(db) == []


def test_request_id_propagation(client, db):
    rid = "abc-123_DEF.456"
    resp = client.get("/", headers={"X-Request-ID": rid})
    assert resp.headers["x-request-id"] == rid
    assert _metrics(db)[-1].request_id == rid


def test_unsafe_request_ids_are_replaced(client, db):
    for bad in ["short", "<script>alert(1)</script>", "a" * 300, "spaces are bad id", "-leading"]:
        resp = client.get("/", headers={"X-Request-ID": bad})
        got = resp.headers["x-request-id"]
        assert got != bad
        assert len(got) == 32 and all(c in "0123456789abcdef" for c in got)
    # no header at all: one is generated
    assert len(client.get("/").headers["x-request-id"]) == 32
    assert accept_request_id("good-request-id") == "good-request-id"
    assert accept_request_id(None) is None


def test_error_grouping(client, db):
    r1 = client.get("/boom/1", headers={"X-Request-ID": "req-one-0001"})
    r2 = client.get("/boom/2")
    assert r1.status_code == 500 and r2.status_code == 500
    client.get("/other-boom")
    with db.session() as s:
        groups = list(s.scalars(select(ErrorGroup).order_by(ErrorGroup.id)))
        events = list(s.scalars(select(ErrorEvent).order_by(ErrorEvent.id)))
    assert len(groups) == 2
    g = groups[0]
    assert g.exc_type == "builtins.ValueError"
    assert g.count == 2
    assert g.status == "open"
    assert "0x?" in g.message and "0x7" not in g.message
    assert "_raise_value_error" in g.top_frame
    assert "Traceback" in g.sample_traceback
    assert groups[1].exc_type == "builtins.KeyError" and groups[1].count == 1
    assert [e.route for e in events] == ["GET /boom/{n}", "GET /boom/{n}", "GET /other-boom"]
    assert events[0].request_id == "req-one-0001"
    # failed requests are recorded as 500 metrics
    assert [(m.route, m.status) for m in _metrics(db)] == [
        ("/boom/{n}", 500),
        ("/boom/{n}", 500),
        ("/other-boom", 500),
    ]


def test_handled_http_errors_are_not_error_groups(client, db):
    assert client.get("/teapot").status_code == 418
    with db.session() as s:
        assert s.scalar(select(ErrorGroup)) is None
    assert _metrics(db)[-1].status == 418


def test_resolved_error_reopens_on_regression(client, db):
    client.get("/boom/1")
    with db.session() as s:
        gid = s.scalar(select(ErrorGroup.id))
    store.set_error_status(db, gid, "resolved")
    client.get("/boom/3")
    with db.session() as s:
        g = s.get(ErrorGroup, gid)
        assert g.status == "open" and g.count == 2


def test_storage_failures_never_break_requests(client, db, monkeypatch):
    def broken(*a, **k):
        raise RuntimeError("db down")

    monkeypatch.setattr("redblue.observe.middleware.record_request", broken)
    monkeypatch.setattr("redblue.observe.middleware.capture_exception", broken)
    assert client.get("/").status_code == 200
    assert client.get("/boom/1").status_code == 500  # original error still propagates


def test_sample_rate_keeps_errors(make_app):
    app = make_app(sample_rate=0.0)
    c = TestClient(app, raise_server_exceptions=False)
    c.get("/")
    c.get("/boom/1")
    with app.state.rb.db.session() as s:
        rows = list(s.scalars(select(RequestMetric)))
    assert [(r.route, r.status) for r in rows] == [("/boom/{n}", 500)]


def test_metrics_carry_current_deploy(client, db):
    d = store.record_deploy(db, "abc123", "first")
    client.get("/")
    assert _metrics(db)[-1].deploy_id == d.id


def test_json_logs_include_request_id(make_app):
    app = make_app()
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.addFilter(RequestIdFilter())
    handler.setFormatter(JSONFormatter())
    logger = logging.getLogger("test.observe")
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)

    @app.get("/logs")
    def logs() -> dict:
        logger.info("hello %s", "world", extra={"order_id": 7})
        return {}

    try:
        TestClient(app).get("/logs", headers={"X-Request-ID": "trace-me-12345"})
    finally:
        logger.removeHandler(handler)
    line = json.loads(stream.getvalue().strip().splitlines()[-1])
    assert line["msg"] == "hello world"
    assert line["request_id"] == "trace-me-12345"
    assert line["order_id"] == 7
    assert line["level"] == "INFO"
    assert request_id_var.get() is None


def test_configure_logging_is_idempotent():
    from redblue.observe.logging import configure_logging

    root = logging.getLogger()
    before = list(root.handlers)
    level = root.level
    try:
        configure_logging("INFO", stream=io.StringIO())
        h = configure_logging("DEBUG", json=False, stream=io.StringIO())
        ours = [x for x in root.handlers if x not in before]
        assert ours == [h]
    finally:
        for x in list(root.handlers):
            if x not in before:
                root.removeHandler(x)
        root.setLevel(level)
