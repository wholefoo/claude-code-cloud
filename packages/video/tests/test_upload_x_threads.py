import json
import os
import re
import stat
import time

import httpx
import pytest
from pydantic import SecretStr

from redblue.video import clients
from redblue.video import upload_social as social
from redblue.video.models import Publication, Upload, VideoProject
from redblue.video.pipeline import Pipeline

VIDEO = b"0123456789abcdefghij" * 3  # 60 bytes
SECRET = "s" * 48
BASE = "https://video.example.com"


def _settings(vsettings, tmp_path, **extra):
    return vsettings.model_copy(
        update={
            "upload_enabled": True,
            "x_client_id": "x-client",
            "x_client_secret": SecretStr("x-secret"),
            "x_refresh_token": SecretStr("x-refresh-0"),
            "x_token_file": tmp_path / "x.token",
            "threads_access_token": SecretStr("th-token"),
            "threads_user_id": "3300",
            **extra,
        }
    )


def _project(db, s, status="approved") -> VideoProject:
    s.output_dir.mkdir(parents=True, exist_ok=True)
    renders = {}
    for key, slug in (("9:16", "9x16"), ("16:9", "16x9")):
        f = s.output_dir / f"video-1-{slug}.mp4"
        f.write_bytes(VIDEO)
        renders[key] = str(f)
    p = VideoProject(
        topic="Heat pumps",
        status=status,
        renders=renders,
        script={
            "title": "Heat pumps explained",
            "hook": "h",
            "cta": "c",
            "hashtags": ["energy", "home", "climate", "extra"],
            "beats": [
                {"narration": "a b", "visual_query": "q", "seconds": 3},
                {"narration": "c d", "visual_query": "q", "seconds": 3},
            ],
        },
    )
    db.add(p)
    db.flush()
    return p


class Fake:
    def __init__(self, fetch=None):
        self.calls: list[httpx.Request] = []
        self.refreshes = 0
        self.x_state = "pending"
        self.th_status = "IN_PROGRESS"
        self.fetch = fetch  # simulates Meta fetching video_url
        self.fetched: list[bytes] = []

    def paths(self) -> list[str]:
        return [f"{r.method} {r.url.host}{r.url.path}" for r in self.calls]

    def handler(self, req: httpx.Request) -> httpx.Response:
        self.calls.append(req)
        host, path = req.url.host, req.url.path
        if host == "api.x.com" and path == "/2/oauth2/token":
            form = dict(x.split("=", 1) for x in req.content.decode().split("&"))
            assert form["grant_type"] == "refresh_token" and form["client_id"] == "x-client"
            assert form["refresh_token"] == f"x-refresh-{self.refreshes}"  # single use
            assert req.headers["authorization"].startswith("Basic ")
            self.refreshes += 1
            return httpx.Response(
                200,
                json={
                    "access_token": "x-access",
                    "refresh_token": f"x-refresh-{self.refreshes}",
                },
            )
        if host == "api.x.com":
            assert req.headers["authorization"] == "Bearer x-access"
            if path == "/2/media/upload/initialize":
                body = json.loads(req.content)
                assert body == {
                    "media_type": "video/mp4",
                    "total_bytes": len(VIDEO),
                    "media_category": "tweet_video",
                }
                return httpx.Response(200, json={"data": {"id": "1880000000000000001"}})
            if path == "/2/media/upload/1880000000000000001/append":
                assert b'name="segment_index"' in req.content and b'name="media"' in req.content
                return httpx.Response(200, json={})
            if path == "/2/media/upload/1880000000000000001/finalize":
                return httpx.Response(
                    200,
                    json={
                        "data": {
                            "id": "1880000000000000001",
                            "processing_info": {"state": "pending", "check_after_secs": 1},
                        }
                    },
                )
            if path == "/2/media/upload" and req.method == "GET":
                assert req.url.params["command"] == "STATUS"
                info = {"state": self.x_state}
                if self.x_state == "failed":
                    info["error"] = {"message": "InvalidMedia"}
                return httpx.Response(200, json={"data": {"processing_info": info}})
            if path == "/2/tweets":
                return httpx.Response(
                    201, json={"data": {"id": "1880000000000000999", "text": "t"}}
                )
        if host == "graph.threads.net":
            assert req.headers["authorization"] == "Bearer th-token"
            if path == "/v1.0/3300/threads":
                form = dict(x.split("=", 1) for x in req.content.decode().split("&"))
                assert form["media_type"] == "VIDEO"
                if self.fetch:
                    self.fetched.append(
                        self.fetch(httpx.URL(httpx.QueryParams(req.content.decode())["video_url"]))
                    )
                return httpx.Response(200, json={"id": "17800000001"})
            if path == "/v1.0/17800000001":
                return httpx.Response(
                    200, json={"status": self.th_status, "error_message": "bad video"}
                )
            if path == "/v1.0/3300/threads_publish":
                return httpx.Response(200, json={"id": "17800000002"})
            if path == "/v1.0/17800000002":
                return httpx.Response(
                    200, json={"permalink": "https://www.threads.net/@heatfan/post/AbC"}
                )
        return httpx.Response(404, json={"error": {"message": f"unexpected {path}"}})

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self.handler))


def test_x_needs_a_token_file_and_keeps_rotated_tokens(vsettings, tmp_path):
    s = _settings(vsettings, tmp_path)
    assert s.x_credentials == ("x-client", "x-secret", "x-refresh-0")  # seeded from env
    assert _settings(vsettings, tmp_path, x_token_file=None).x_credentials is None
    (tmp_path / "x.token").write_text("x-refresh-7")
    assert s.x_credentials[2] == "x-refresh-7"  # the file wins once it exists


def test_x_upload_then_person_posts(platform, vsettings, tmp_path, monkeypatch):
    monkeypatch.setattr(clients.XClient, "CHUNK", 25)  # 60 bytes → 3 segments
    s, fake = _settings(vsettings, tmp_path), Fake()
    token_file = tmp_path / "x.token"
    with platform.db.session() as db:
        p = _project(db, s)
        d = social.defaults(p)
        assert d["x_text"] == "Heat pumps explained #energy #home #climate (AI-assisted)"
        up = social.upload_x(
            db,
            p,
            social.XRequest(text=d["x_text"]),
            s,
            confirmed_by="ed",
            confirmed=True,
            http=fake.client(),
        )
        assert (up.status, up.format, up.external_ref) == (
            "processing",
            "16:9",
            "1880000000000000001",
        )
        appends = [r for r in fake.calls if r.url.path.endswith("/append")]
        assert [
            re.search(rb'name="segment_index"\r\n\r\n(\d+)', r.content).group(1) for r in appends
        ] == [b"0", b"1", b"2"]
        assert token_file.read_text() == "x-refresh-1"
        assert stat.S_IMODE(os.stat(token_file).st_mode) == 0o600

        # Tracking reads status (rotating the token again) but never posts.
        fake.x_state = "succeeded"
        Pipeline(platform, s, fake.client()).track(db)
        assert up.status == "ready" and not any(c.endswith("/2/tweets") for c in fake.paths())
        assert token_file.read_text() == "x-refresh-2"

        with pytest.raises(ValueError, match="Confirm"):
            social.publish(db, up, s, confirmed_by="chief", confirmed=False, http=fake.client())
        social.publish(db, up, s, confirmed_by="chief", confirmed=True, http=fake.client())
        assert up.status == "published" and up.url == "https://x.com/i/status/1880000000000000999"
        post = json.loads(next(r for r in fake.calls if r.url.path == "/2/tweets").content)
        assert post == {"text": d["x_text"], "media": {"media_ids": ["1880000000000000001"]}}
        assert db.query(Publication).filter_by(platform="x").one().uploaded_by == "chief"
        with pytest.raises(ValueError, match="already posted"):
            social.publish(db, up, s, confirmed_by="chief", confirmed=True, http=fake.client())
        assert sum(c.endswith("/2/tweets") for c in fake.paths()) == 1


def test_x_limits_and_failed_processing(platform, vsettings, tmp_path):
    s, fake = _settings(vsettings, tmp_path), Fake()
    with platform.db.session() as db:
        p = _project(db, s)
        with pytest.raises(ValueError, match="280 characters"):
            social.upload_x(
                db,
                p,
                social.XRequest(text="x" * 281),
                s,
                confirmed_by="ed",
                confirmed=True,
                http=fake.client(),
            )
        assert fake.calls == []
        up = social.upload_x(
            db,
            p,
            social.XRequest(text="hi"),
            s,
            confirmed_by="ed",
            confirmed=True,
            http=fake.client(),
        )
        fake.x_state = "failed"
        social.refresh(db, up, s, fake.client())
        assert up.status == "failed" and up.error == "InvalidMedia"


@pytest.mark.parametrize(
    "url, ok",
    [
        ("https://video.example.com", True),
        ("https://203.0.114.9", True),
        ("http://video.example.com", False),
        ("https://localhost:8000", False),
        ("https://10.0.0.5", False),
        ("https://192.168.1.2", False),
        ("https://box.local", False),
        ("", False),
    ],
)
def test_threads_needs_a_public_https_address(url, ok):
    assert (social.public_base_problem(url) is None) is ok


def _sig(link: str) -> str:
    return link.rsplit("/", 1)[1].removesuffix(".mp4")


def test_media_links_are_signed_scoped_and_short_lived():
    exp = int(time.time()) + 600
    link = social.media_link(BASE, SECRET, 5, "9:16", exp)
    assert link.startswith(f"{BASE}/video-media/5/9x16/{exp}/")
    sig = _sig(link)
    assert social.verify_media_link(SECRET, 5, "9x16", exp, sig)
    assert not social.verify_media_link(SECRET, 6, "9x16", exp, sig)  # another project
    assert not social.verify_media_link(SECRET, 5, "16x9", exp, sig)  # another render
    assert not social.verify_media_link("other" * 10, 5, "9x16", exp, sig)
    assert not social.verify_media_link(SECRET, 5, "9x16", exp + 1, sig)
    old = int(time.time()) - 1
    assert not social.verify_media_link(
        SECRET, 5, "9x16", old, _sig(social.media_link(BASE, SECRET, 5, "9:16", old))
    )
    far = int(time.time()) + 86400  # links can't be minted for longer than an hour
    assert not social.verify_media_link(
        SECRET, 5, "9x16", far, _sig(social.media_link(BASE, SECRET, 5, "9:16", far))
    )


def test_threads_failed_container_leaves_no_upload(platform, vsettings, tmp_path):
    s = _settings(vsettings, tmp_path, threads_user_id="3301")  # unknown user → 404
    with platform.db.session() as db:
        p = _project(db, s)
        with pytest.raises(clients.PlatformError):
            social.upload_threads(
                db,
                p,
                social.ThreadsRequest(text="hi"),
                s,
                public_base=BASE,
                secret=SECRET,
                confirmed_by="ed",
                confirmed=True,
                http=Fake().client(),
            )
        assert db.query(Upload).count() == 0
        with pytest.raises(ValueError, match="local address"):
            social.upload_threads(
                db,
                p,
                social.ThreadsRequest(),
                s,
                public_base="https://localhost",
                secret=SECRET,
                confirmed_by="ed",
                confirmed=True,
                http=Fake().client(),
            )


@pytest.mark.parametrize(
    "cmd", [["x", "1"], ["x-publish", "1"], ["threads", "1"], ["threads-publish", "1"]]
)
def test_cli_commands_need_a_person(tmp_path, monkeypatch, cmd):
    from typer.testing import CliRunner

    from redblue.cli.main import app

    monkeypatch.setenv("RB_DATABASE_URL", f"sqlite:///{tmp_path}/c.db")
    r = CliRunner().invoke(app, ["video", *cmd], input="y\n")
    assert r.exit_code != 0 and "person at the keyboard" in r.output


def test_admin_x_and_threads_flow_with_media_link(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from redblue.core.config import Settings
    from redblue.platform.app import create_app
    from redblue.platform.seed import ensure_admin
    from redblue.video import config as vconfig

    monkeypatch.chdir(tmp_path)
    for k, v in {
        "RB_VIDEO_OUTPUT_DIR": str(tmp_path / "out"),
        "RB_VIDEO_UPLOAD_ENABLED": "true",
        "RB_VIDEO_PUBLIC_BASE_URL": BASE,
        "X_CLIENT_ID": "x-client",
        "X_CLIENT_SECRET": "x-secret",
        "X_REFRESH_TOKEN": "x-refresh-0",
        "RB_VIDEO_X_TOKEN_FILE": str(tmp_path / "x.token"),
        "THREADS_ACCESS_TOKEN": "th-token",
        "THREADS_USER_ID": "3300",
    }.items():
        monkeypatch.setenv(k, v)
    vconfig.get_video_settings.cache_clear()
    app = create_app(
        Settings(
            database_url=f"sqlite:///{tmp_path}/t.db",
            env="test",
            storage_dir=tmp_path / "m",
            secret_key=SECRET,
        )
    )
    c = TestClient(app)
    public = TestClient(app)  # Meta's fetcher: no session, no cookies

    def meta_fetch(url: httpx.URL) -> bytes:
        assert f"{url.scheme}://{url.host}" == BASE
        r = public.get(url.path)
        assert r.status_code == 200 and r.headers["cache-control"] == "private, no-store"
        return r.content

    fake = Fake(fetch=meta_fetch)
    mock = fake.client()
    monkeypatch.setattr(clients, "_client", lambda cl: cl or mock)
    with app.state.rb.db.session() as db:
        ensure_admin(db, "admin@example.com", "correct-horse-battery")
        pid = _project(db, vconfig.get_video_settings()).id
    c.get("/admin/login")
    tok = c.cookies["rb_csrf"]
    c.post(
        "/admin/login",
        data={"email": "admin@example.com", "password": "correct-horse-battery", "csrf_token": tok},
    )
    url = f"/admin/video/projects/{pid}"
    page = c.get(url).text
    assert "Upload to X" in page and "Send to Threads (not posted yet)" in page

    r = c.post(f"{url}/threads", data={"csrf_token": tok, "text": "Heat pumps", "confirm": "1"})
    assert "Sent to Threads" in r.text
    assert fake.fetched == [VIDEO]  # Meta fetched the render through the signed link
    with app.state.rb.db.session() as db:
        th = db.query(Upload).filter_by(platform="threads").one()
        th_id, expires = th.id, th.meta["media_expires"]
    link_path = next(
        httpx.URL(httpx.QueryParams(r.content.decode())["video_url"]).path
        for r in fake.calls
        if r.url.path == "/v1.0/3300/threads"
    )
    assert public.get(link_path.replace(f"/{expires}/", f"/{expires + 1}/")).status_code == 404
    assert public.get(f"/video-media/{pid}/9x16/{expires}/{'0' * 40}.mp4").status_code == 404
    assert public.get(link_path.replace("/9x16/", "/16x9/")).status_code == 404

    fake.th_status = "FINISHED"
    r = c.post(f"/admin/video/uploads/{th_id}/refresh", data={"csrf_token": tok})
    assert "Threads upload: ready" in r.text and "Post to Threads" in r.text
    assert not any("threads_publish" in p for p in fake.paths())
    r = c.post(f"/admin/video/uploads/{th_id}/publish", data={"csrf_token": tok, "confirm": "1"})
    assert "Published on Threads: https://threads.net/@heatfan/post/AbC" in r.text
    with app.state.rb.db.session() as db:  # only approved projects are ever served
        db.get(VideoProject, pid).status = "rejected"
    assert public.get(link_path).status_code == 404
    with app.state.rb.db.session() as db:
        db.get(VideoProject, pid).status = "approved"

    r = c.post(f"{url}/x", data={"csrf_token": tok, "format": "16:9", "text": "Hi", "confirm": "1"})
    assert "Uploaded to X (not posted yet)" in r.text
    with app.state.rb.db.session() as db:
        x_id = db.query(Upload).filter_by(platform="x").one().id
    fake.x_state = "succeeded"
    r = c.post(f"/admin/video/uploads/{x_id}/publish", data={"csrf_token": tok})
    assert "Confirm that this should be posted on X" in r.text
    r = c.post(f"/admin/video/uploads/{x_id}/publish", data={"csrf_token": tok, "confirm": "1"})
    assert "Published on X: https://x.com/i/status/1880000000000000999" in r.text
    vconfig.get_video_settings.cache_clear()


def test_threads_panel_explains_local_address(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from redblue.core.config import Settings
    from redblue.platform.app import create_app
    from redblue.platform.seed import ensure_admin
    from redblue.video import config as vconfig

    monkeypatch.chdir(tmp_path)
    for k, v in {
        "RB_VIDEO_OUTPUT_DIR": str(tmp_path / "out"),
        "RB_VIDEO_UPLOAD_ENABLED": "true",
        "THREADS_ACCESS_TOKEN": "th-token",
        "THREADS_USER_ID": "3300",
    }.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("RB_VIDEO_PUBLIC_BASE_URL", raising=False)
    vconfig.get_video_settings.cache_clear()
    app = create_app(
        Settings(
            database_url=f"sqlite:///{tmp_path}/t.db",
            env="test",
            storage_dir=tmp_path / "m",
            secret_key=SECRET,
        )
    )
    with app.state.rb.db.session() as db:
        ensure_admin(db, "admin@example.com", "correct-horse-battery")
        pid = _project(db, vconfig.get_video_settings()).id
    c = TestClient(app)
    c.get("/admin/login")
    tok = c.cookies["rb_csrf"]
    c.post(
        "/admin/login",
        data={"email": "admin@example.com", "password": "correct-horse-battery", "csrf_token": tok},
    )
    page = c.get(f"/admin/video/projects/{pid}").text
    assert "public https address" in page and "Send to Threads" not in page
    vconfig.get_video_settings.cache_clear()
