import pytest
from fastapi import Response
from fastapi.testclient import TestClient

from redblue.core.app import create_core_app
from redblue.core.config import Settings


@pytest.fixture
def settings(tmp_path):
    return Settings(database_url="sqlite://", env="test", storage_dir=tmp_path / "media",
                    secret_key="x" * 48, base_url="http://testserver")


@pytest.fixture
def app(settings):
    from redblue.core import auth

    app = create_core_app(settings)

    @app.post("/_test/login/{uid}")
    def _login(uid: int, response: Response):
        with app.state.rb.db.session() as s:
            auth.login(_FakeReq(app), response, s.get(auth.User, uid))
        return {"ok": True}

    return app


class _FakeReq:
    def __init__(self, app):
        self.app = app


@pytest.fixture
def client(app):
    c = TestClient(app)
    c.get("/_rb/health")
    c.headers["x-csrf-token"] = c.cookies["rb_csrf"]
    return c
