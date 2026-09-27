import re

import pytest
from fastapi.testclient import TestClient

from redblue.core.config import Settings
from redblue.core.security import limiter
from redblue.platform.app import create_app
from redblue.platform.seed import ensure_admin, seed


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    limiter.reset()
    s = Settings(
        database_url=f"sqlite:///{tmp_path}/t.db",
        env="test",
        storage_dir=tmp_path / "media",
        secret_key="k" * 48,
        base_url="http://testserver",
        email_from="owner@example.com",
    )
    app = create_app(s)
    with app.state.rb.db.session() as db:
        admin = ensure_admin(db, "admin@example.com", "correct-horse-battery")
        seed(db, admin)
    return app


@pytest.fixture
def client(app):
    c = TestClient(app)
    c.get("/")
    c.headers["x-csrf-token"] = c.cookies["rb_csrf"]
    return c


@pytest.fixture
def admin_client(client):
    r = client.post(
        "/admin/login",
        data={
            "email": "admin@example.com",
            "password": "correct-horse-battery",
            "csrf_token": client.cookies["rb_csrf"],
        },
        follow_redirects=False,
    )
    assert r.status_code == 303, r.text
    return client


def csrf(client):
    return client.cookies["rb_csrf"]


def form_id(html: str) -> int:
    return int(re.search(r"/admin/content/(\d+)", html).group(1))
