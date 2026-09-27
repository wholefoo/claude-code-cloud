from fastapi.testclient import TestClient

from redblue.cms.agent import ContentAgent
from redblue.cms.api import cms_api_router
from redblue.cms.models import Status
from redblue.core.app import create_core_app
from redblue.core.auth import Role, create_api_key, create_user
from redblue.core.config import Settings


def test_headless_api_and_content_agent(tmp_path):
    app = create_core_app(Settings(database_url="sqlite://", env="test", storage_dir=tmp_path))
    app.include_router(cms_api_router())
    rb = app.state.rb
    with rb.db.session() as s:
        pub = create_user(s, "p@example.com", None, Role.publisher)
        key = create_api_key(s, pub, "test")
    c = TestClient(app)
    h = {"Authorization": f"Bearer {key}"}
    r = c.post(
        "/api/cms/post",
        json={"title": "API post", "blocks": [{"type": "paragraph", "text": "hi"}]},
        headers=h,
    )
    assert r.status_code == 201, r.text
    eid = r.json()["id"]
    assert c.get("/api/cms/post/api-post").status_code == 404  # not live yet
    for action in ("submit", "approve", "publish"):
        assert (
            c.post(
                f"/api/cms/entries/{eid}/transition", json={"action": action}, headers=h
            ).status_code
            == 200
        )
    body = c.get("/api/cms/post/api-post").json()
    assert body["path"] == "/blog/api-post" and body["blocks"][0]["text"] == "hi"
    assert c.post("/api/cms/post", json={"title": "x"}).status_code in (401, 403)
    bad = c.post(
        "/api/cms/post",
        json={"title": "x", "blocks": [{"type": "paragraph", "text": "[a](javascript:1)"}]},
        headers=h,
    )
    assert bad.status_code == 422

    with rb.db.session() as s:
        e = ContentAgent(rb).draft(s, "guide", "How to brew coffee", ["How do I brew coffee?"])
        assert e.status == Status.draft and e.ai_generated and e.blocks[0]["type"] == "answer"
