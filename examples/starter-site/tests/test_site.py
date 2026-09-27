from fastapi.testclient import TestClient


def test_home_renders_with_security_headers(tmp_path, monkeypatch):
    monkeypatch.setenv("RB_DATABASE_URL", f"sqlite:///{tmp_path}/t.db")
    monkeypatch.setenv("RB_ENV", "test")
    monkeypatch.setenv("RB_STORAGE_DIR", str(tmp_path / "media"))
    from redblue.core.config import get_settings

    get_settings.cache_clear()
    import importlib

    import main

    importlib.reload(main)
    r = TestClient(main.app).get("/")
    assert r.status_code == 200
    assert "content-security-policy" in r.headers
