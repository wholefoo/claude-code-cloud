from __future__ import annotations

import pytest
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.testclient import TestClient
from redblue.core.app import create_core_app
from redblue.core.auth import User, login
from redblue.core.config import Settings
from redblue.observe import install, observe_router
from redblue.observe.store import _deploy_cache


def _raise_value_error(n: int) -> None:
    raise ValueError(f"bad value {n} at {object()!r}")


def build_app(tmp_path, **install_kwargs) -> FastAPI:
    settings = Settings(database_url="sqlite://", env="test", storage_dir=tmp_path)
    app = create_core_app(settings)
    install(app, configure_logs=False, **install_kwargs)
    app.include_router(observe_router(), prefix="/admin/observe")

    @app.get("/")
    def home() -> dict:
        return {"ok": True}

    @app.get("/items/{item_id}")
    def item(item_id: int) -> dict:
        return {"id": item_id}

    @app.get("/boom/{n}")
    def boom(n: int) -> dict:
        _raise_value_error(n)
        return {}

    @app.get("/other-boom")
    def other_boom() -> dict:
        raise KeyError("missing")

    @app.get("/teapot")
    def teapot() -> dict:
        raise HTTPException(status_code=418)

    @app.get("/_test/login/{uid}")
    def test_login(uid: int, request: Request, response: Response) -> dict:
        with request.app.state.rb.db.session() as s:
            user = s.get(User, uid)
        login(request, response, user)
        return {"ok": True}

    return app


@pytest.fixture(autouse=True)
def _clear_deploy_cache():
    _deploy_cache.clear()
    yield
    _deploy_cache.clear()


@pytest.fixture
def app(tmp_path) -> FastAPI:
    return build_app(tmp_path)


@pytest.fixture
def db(app):
    return app.state.rb.db


@pytest.fixture
def client(app) -> TestClient:
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def make_app(tmp_path):
    def factory(**install_kwargs) -> FastAPI:
        return build_app(tmp_path, **install_kwargs)

    return factory
