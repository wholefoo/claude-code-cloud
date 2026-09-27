from fastapi.testclient import TestClient

from app import app


def test_index():
    assert TestClient(app).get("/").status_code == 200
