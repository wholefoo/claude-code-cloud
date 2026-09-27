"""DELIBERATELY VULNERABLE demo app for exercising the RedBlue gate. DO NOT DEPLOY.

Every bug is labelled PLANTED so the gate's evals can check it is found.
"""

import sqlite3

import yaml
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

app = FastAPI(title="Vulnerable demo (do not deploy)")
templates = Jinja2Templates(directory="templates")

API_TOKEN = "q7Vm2Kx9Lp4Wz8Rt1Nc6Hb3Jd5Gs0Yf"  # PLANTED: hardcoded secret

db = sqlite3.connect(":memory:", check_same_thread=False)
db.execute("CREATE TABLE products (id INTEGER PRIMARY KEY, name TEXT)")
db.executemany("INSERT INTO products (name) VALUES (?)", [("Lamp",), ("Desk",), ("Chair",)])
COMMENTS: list[str] = ["First!"]


@app.get("/")
def index():
    return HTMLResponse("<h1>Demo shop</h1><p><a href='/search?q=lamp'>Search</a></p>")


@app.get("/search")
def search(q: str = ""):
    rows = db.execute(f"SELECT name FROM products WHERE name LIKE '%{q}%'").fetchall()  # PLANTED: SQLi
    items = "".join(f"<li>{r[0]}</li>" for r in rows)
    return HTMLResponse(f"<h1>Results for {q}</h1><ul>{items}</ul>")  # PLANTED: reflected XSS


@app.get("/go")
def go(next: str = "/"):
    return RedirectResponse(next)  # PLANTED: open redirect


@app.post("/import")
async def import_config(request: Request):
    data = yaml.load(await request.body(), Loader=yaml.Loader)  # PLANTED: unsafe YAML
    return {"keys": list(data or {})}


@app.get("/comments")
def comments(request: Request):
    # PLANTED: comments rendered with |safe in templates/comments.html (stored XSS)
    return templates.TemplateResponse(request, "comments.html", {"comments": COMMENTS})
