from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from redblue.observe import UPTIME_JOB, install
from redblue.observe.models import UptimeCheck
from redblue.observe.otel import setup_otel, span
from redblue.observe.uptime import run_checks, validate_path

BAD = [
    "https://evil.example/",
    "http://localhost:8000/admin",
    "//evil.example/x",
    "javascript:alert(1)",
    "pricing",
    "",
    "/\\evil",
    "/a\nb",
]


@pytest.mark.parametrize("path", BAD)
def test_rejects_urls_and_bad_paths(path):
    with pytest.raises(ValueError):
        validate_path(path)


def test_accepts_paths():
    assert validate_path("/pricing?x=1") == "/pricing?x=1"


def test_run_checks_validates_before_sending(db):
    class Exploding:
        def get(self, *a, **k):
            raise AssertionError("must not send")

    with pytest.raises(ValueError):
        run_checks(db, ["/", "https://evil.example/"], client=Exploding())
    with pytest.raises(ValueError):
        run_checks(db, ["/"])  # neither client nor base_url


def test_run_checks_records_results(client, db):
    results = run_checks(db, ["/", "/_rb/health", "/missing", "/boom/1"], client=client)
    assert [(r.path, r.status, r.ok) for r in results] == [
        ("/", 200, True),
        ("/_rb/health", 200, True),
        ("/missing", 404, False),
        ("/boom/1", 500, False),
    ]
    with db.session() as s:
        rows = list(s.scalars(select(UptimeCheck).order_by(UptimeCheck.id)))
    assert [r.ok for r in rows] == [True, True, False, False]
    assert rows[2].error == "HTTP 404"


def test_network_errors_are_failures(db):
    class Down:
        def get(self, *a, **k):
            raise ConnectionError("refused")

    (r,) = run_checks(db, ["/"], client=Down())
    assert not r.ok and r.status == 0 and "refused" in r.error


def test_install_schedules_uptime_job(app):
    jobs = app.state.rb.jobs
    assert UPTIME_JOB in jobs.handlers
    assert [n for n, _, _ in jobs.schedules].count(UPTIME_JOB) == 1
    install(app, configure_logs=False)  # idempotent schedule
    assert [n for n, _, _ in jobs.schedules].count(UPTIME_JOB) == 1


def test_install_rejects_url_uptime_paths(make_app):
    with pytest.raises(ValueError):
        make_app(uptime_paths=["https://evil.example/"])


def test_otel_is_noop_without_packages(app):
    assert setup_otel(app, "svc") is False
    with span("job.run", n=1, obj=object()) as sp:
        assert sp is None
    assert app.state.rb.extras["observe"]["otel"] is False
    assert TestClient(app).get("/").status_code == 200
