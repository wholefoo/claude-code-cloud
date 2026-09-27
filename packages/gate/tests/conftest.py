import shutil
from pathlib import Path

import pytest

DEMO = Path(__file__).resolve().parents[3] / "examples" / "vulnerable-demo"


@pytest.fixture
def demo(tmp_path):
    dest = tmp_path / "demo"
    shutil.copytree(DEMO, dest, ignore=shutil.ignore_patterns("__pycache__", ".redblue"))
    return dest


@pytest.fixture
def hardened(tmp_path):
    d = tmp_path / "hardened"
    d.mkdir()
    (d / "app.py").write_text('''
from html import escape

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, RedirectResponse

app = FastAPI()


@app.middleware("http")
async def headers(request, call_next):
    resp = await call_next(request)
    resp.headers["Content-Security-Policy"] = "default-src 'self'; frame-ancestors 'none'"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Referrer-Policy"] = "same-origin"
    return resp


@app.get("/")
def index():
    return HTMLResponse("<h1>Hello</h1>")


@app.get("/search")
def search(q: str = ""):
    return HTMLResponse("<h1>" + escape(q) + "</h1>")


@app.get("/go")
def go(next: str = "/"):
    return RedirectResponse(next if next.startswith("/") and not next.startswith("//") else "/")
''')
    (d / ".redblue.yml").write_text("app: app:app\nscanners:\n  pip_audit: false\n")
    return d
