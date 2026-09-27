import json
import re

from redblue.templates.jsonld import validate_graph
from redblue.templates.registry import CATALOG


def test_pages_render_with_security_headers(client):
    for path in [
        "/",
        "/about",
        "/pricing",
        "/contact",
        "/blog",
        "/blog/hello-world",
        "/glossary",
        "/glossary/answer-engine-optimization",
        "/authors/1",
        "/search?q=secure",
        "/privacy",
    ]:
        r = client.get(path)
        assert r.status_code == 200, path
        assert "'nonce-" in r.headers["content-security-policy"]
        assert r.headers["x-frame-options"] == "DENY"
        html = r.text
        assert html.count("<h1") == 1, path
        assert '<html lang="en">' in html and '<link rel="canonical"' in html
        for raw in re.findall(r'<script type="application/ld\+json"[^>]*>(.*?)</script>', html):
            assert validate_graph(json.loads(raw)) == [], path


def test_404_redirects_and_maintenance(app, client):
    r = client.get("/does-not-exist")
    assert r.status_code == 404 and "Page not found" in r.text and "noindex" in r.text
    from redblue.cms import service

    with app.state.rb.db.session() as db:
        service.add_redirect(db, "/old", "/about")
    r = client.get("/old", follow_redirects=False)
    assert r.status_code == 301 and r.headers["location"] == "/about"
    app.state.rb.settings.maintenance_mode = True
    try:
        r = client.get("/about")
        assert r.status_code == 503 and r.headers["retry-after"]
        assert client.get("/admin/login").status_code == 200
    finally:
        app.state.rb.settings.maintenance_mode = False


def test_seo_outputs(client):
    assert "<loc>http://testserver/blog/hello-world</loc>" in client.get("/sitemaps/post.xml").text
    assert "/sitemaps/page.xml" in client.get("/sitemap.xml").text
    robots = client.get("/robots.txt").text
    assert "User-agent: GPTBot" in robots and "Sitemap: http://testserver/sitemap.xml" in robots
    llms = client.get("/llms.txt").text
    assert llms.startswith("# ") and "[Hello, world](http://testserver/blog/hello-world)" in llms
    assert "<rss" in client.get("/feed.xml").text
    svg = client.get("/_rb/og/blog/hello-world.svg")
    assert svg.headers["content-type"].startswith("image/svg") and "Hello, world" in svg.text


def test_structured_data_types(client):
    html = client.get("/pricing").text
    assert '"@type":"FAQPage"' in html
    html = client.get("/glossary/answer-engine-optimization").text
    assert '"@type":"DefinedTerm"' in html


def test_catalog_covers_plan():
    assert len(CATALOG) >= 58
    for key, pt in CATALOG.items():
        assert pt.template.startswith("pages/"), key


def test_jsonld_cannot_break_out_of_script(app, client):
    from redblue.cms import service
    from redblue.core.auth import User

    with app.state.rb.db.session() as db:
        admin = db.get(User, 1)
        e = service.create_entry(
            db,
            "post",
            service.EntryInput(
                title="</script><script>alert(1)</script>",
                summary="x",
                blocks=[{"type": "paragraph", "text": "<img src=x onerror=alert(1)>"}],
            ),
            admin,
        )
        service.submit_for_review(db, e, admin)
        service.approve(db, e, admin)
        service.publish(db, e, admin)
        slug = e.slug
    html = client.get(f"/blog/{slug}").text
    assert "<script>alert(1)" not in html and "<img src=x" not in html


def test_favicon_only_embeds_safe_values(client):
    import pytest

    from redblue.templates.site import favicon_svg

    svg = favicon_svg('"><script>alert(1)</script>', "#123456", "#ffffff")
    assert "<script" not in svg and ">S</text>" in svg
    assert ">R</text>" in favicon_svg(
        "ümlaut-only ☃", "#123", "#fff"
    ) or ">M</text>" in favicon_svg("ümlaut-only ☃", "#123", "#fff")
    with pytest.raises(ValueError):
        favicon_svg("x", 'red" onload="alert(1)', "#fff")
    r = client.get("/_rb/favicon.svg")
    assert r.status_code == 200 and r.headers["content-type"].startswith("image/svg+xml")
