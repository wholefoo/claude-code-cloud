import pytest

from redblue.core import security, storage, webhooks
from redblue.core.email import Email


def test_security_headers(client):
    r = client.get("/_rb/health")
    csp = r.headers["content-security-policy"]
    assert "default-src 'self'" in csp and "'nonce-" in csp and "frame-ancestors 'none'" in csp
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["x-frame-options"] == "DENY"


def test_csrf_blocks_missing_and_wrong_tokens(app, client):
    @app.post("/thing")
    def thing():
        return {"ok": True}

    assert client.post("/thing").status_code == 200
    assert client.post("/thing", headers={"x-csrf-token": "nope"}).status_code == 403
    del client.headers["x-csrf-token"]
    assert client.post("/thing").status_code == 403
    assert client.post("/thing", data={"csrf_token": client.cookies["rb_csrf"]}).status_code == 200


@pytest.mark.parametrize("url,ok", [
    ("/about", True), ("https://example.com", True), ("mailto:a@b.co", True),
    ("javascript:alert(1)", False), ("JaVaScRiPt:alert(1)", False), ("data:text/html,x", False),
    ("//evil.com", False), ("java\tscript:alert(1)", False),
])
def test_is_safe_link(url, ok):
    assert security.is_safe_link(url) is ok


@pytest.mark.parametrize("target,expected", [
    ("/admin", "/admin"), ("https://evil.com", "/"), ("//evil.com", "/"), ("/\\evil.com", "/"),
    (None, "/"), ("admin", "/"),
])
def test_safe_redirect(target, expected):
    assert security.safe_redirect_target(target) == expected


def test_password_hashing():
    h = security.hash_password("correct horse battery")
    assert security.verify_password(h, "correct horse battery")
    assert not security.verify_password(h, "wrong password!!")
    with pytest.raises(ValueError):
        security.hash_password("short")


def test_rate_limiter():
    rl = security.RateLimiter()
    assert all(rl.hit("k", 3, 60) for _ in range(3))
    assert not rl.hit("k", 3, 60)


def test_upload_validation(tmp_path):
    st = storage.LocalStorage(tmp_path, 1024 * 1024, "s")
    png = b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + (16).to_bytes(4, "big") + (9).to_bytes(4, "big") + b"\x00" * 20
    f = st.put(png, "My Photo!!.png")
    assert f.mime == "image/png" and f.key.startswith("my-photo-") and f.key.endswith(".png")
    assert storage.image_dimensions(png) == (16, 9)
    with pytest.raises(storage.UploadRejected):
        st.put(b"<svg onload=alert(1)>", "x.svg")
    with pytest.raises(storage.UploadRejected):
        st.get("../../etc/passwd")
    url = st.signed_url(f.key)
    q = dict(p.split("=") for p in url.split("?")[1].split("&"))
    assert st.verify_signature(f.key, int(q["expires"]), q["sig"])
    assert not st.verify_signature(f.key, int(q["expires"]), "0" * 64)


def test_webhook_signatures():
    ts, sig = webhooks.sign("s3cret", b'{"a":1}')
    assert webhooks.verify("s3cret", b'{"a":1}', ts, sig)
    assert not webhooks.verify("s3cret", b'{"a":2}', ts, sig)
    assert not webhooks.verify("s3cret", b'{"a":1}', "1000", sig)


def test_email_header_injection_rejected():
    with pytest.raises(ValueError):
        Email(to="a@b.co\r\nBcc: x@y.z", subject="s", text="t")
