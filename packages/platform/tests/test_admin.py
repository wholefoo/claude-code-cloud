import json
import re


def test_admin_requires_login_and_csrf(client):
    r = client.get("/admin/content", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/admin/login?next=")
    bad = client.post(
        "/admin/login",
        data={
            "email": "admin@example.com",
            "password": "wrong-password!!",
            "csrf_token": csrf(client),
        },
    )
    assert bad.status_code == 401
    del client.headers["x-csrf-token"]
    r = client.post(
        "/admin/login", data={"email": "admin@example.com", "password": "correct-horse-battery"}
    )
    assert r.status_code == 403  # no CSRF token


def test_admin_pages_render(admin_client):
    for path in [
        "/admin",
        "/admin/content",
        "/admin/content/new?collection=post",
        "/admin/media",
        "/admin/redirects",
        "/admin/audience",
        "/admin/growth",
        "/admin/growth/social",
        "/admin/experiments",
        "/admin/security",
        "/admin/observe",
        "/admin/templates",
        "/admin/users",
    ]:
        r = admin_client.get(path)
        assert r.status_code == 200, (path, r.text[:500])


def test_content_lifecycle_through_admin(admin_client):
    c = admin_client
    blocks = [
        {
            "type": "answer",
            "question": "What is RedBlue?",
            "answer": "A secure, self-hosted web platform.",
        }
    ]
    r = c.post(
        "/admin/content/new?collection=post",
        data={
            "csrf_token": csrf(c),
            "title": "Admin post",
            "summary": "Made in the admin",
            "blocks": json.dumps(blocks),
            "data": "{}",
            "target_questions": "What is RedBlue?",
        },
        follow_redirects=True,
    )
    assert r.status_code == 200 and "Draft created" in r.text
    eid = int(re.search(r"/admin/content/(\d+)/workflow", r.text).group(1))
    assert c.get("/blog/admin-post").status_code == 404
    assert c.get(f"/admin/content/{eid}/preview").status_code == 200
    for action in ("submit", "approve", "publish"):
        r = c.post(f"/admin/content/{eid}/workflow", data={"csrf_token": csrf(c), "action": action})
        assert r.status_code == 200
    assert "A secure, self-hosted web platform." in c.get("/blog/admin-post").text

    bad = c.post(
        f"/admin/content/{eid}",
        data={
            "csrf_token": csrf(c),
            "title": "Admin post",
            "data": "{}",
            "blocks": json.dumps([{"type": "paragraph", "text": "[x](javascript:alert(1))"}]),
        },
    )
    assert "Please fix" in bad.text and "Unsafe" in bad.text


def test_thin_programmatic_page_blocked(admin_client):
    c = admin_client
    r = c.post(
        "/admin/content/new?collection=location",
        data={
            "csrf_token": csrf(c),
            "title": "Plumber in Springfield",
            "blocks": json.dumps([{"type": "paragraph", "text": "We fix pipes."}]),
            "data": json.dumps({"name": "Acme Plumbing", "city": "Springfield"}),
        },
        follow_redirects=True,
    )
    eid = int(re.search(r"/admin/content/(\d+)/workflow", r.text).group(1))
    for action in ("submit", "approve"):
        c.post(f"/admin/content/{eid}/workflow", data={"csrf_token": csrf(c), "action": action})
    r = c.post(f"/admin/content/{eid}/workflow", data={"csrf_token": csrf(c), "action": "publish"})
    assert "Thin content" in r.text
    assert c.get("/locations/plumber-in-springfield").status_code == 404


def test_media_upload_requires_alt_and_real_images(admin_client):
    c = admin_client
    png = (
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
        + (4).to_bytes(4, "big")
        + (3).to_bytes(4, "big")
        + b"\x00" * 20
    )
    r = c.post(
        "/admin/media",
        data={"csrf_token": csrf(c), "alt": "A tiny square"},
        files={"file": ("pic.png", png, "image/png")},
        follow_redirects=True,
    )
    assert "Uploaded" in r.text
    key = re.search(r"media_key “([^”]+)”", r.text).group(1)
    img = c.get(f"/media/{key}")
    assert img.status_code == 200 and img.headers["content-type"] == "image/png"
    r = c.post(
        "/admin/media",
        data={"csrf_token": csrf(c), "alt": "x"},
        files={"file": ("x.svg", b"<svg onload=alert(1)>", "image/svg+xml")},
        follow_redirects=True,
    )
    assert "Unsupported file type" in r.text


def test_users_and_roles(admin_client, app):
    c = admin_client
    r = c.post(
        "/admin/users", data={"csrf_token": csrf(c), "email": "w@example.com", "role": "writer"}
    )
    pw = re.search(r"shown once\): <code>([^<]+)</code>", r.text).group(1)
    from fastapi.testclient import TestClient

    w = TestClient(app)
    w.get("/")
    w.post(
        "/admin/login",
        data={"email": "w@example.com", "password": pw, "csrf_token": w.cookies["rb_csrf"]},
    )
    assert w.get("/admin/users").status_code == 403
    assert w.get("/admin/content").status_code == 200


def test_growth_audit_and_report(admin_client):
    c = admin_client
    r = c.post("/admin/growth/audit", data={"csrf_token": csrf(c)})
    assert r.status_code == 200 and "SEO/AEO audit" in r.text
    r = c.post("/admin/growth/report", data={"csrf_token": csrf(c)}, follow_redirects=True)
    assert "Growth report generated" in r.text
    r = c.post(
        "/admin/growth/utm",
        data={
            "csrf_token": csrf(c),
            "url": "https://x.com/p",
            "source": "news",
            "medium": "email",
            "campaign": "launch",
        },
    )
    assert "utm_source=news" in r.text


def csrf(client):
    return client.cookies["rb_csrf"]
