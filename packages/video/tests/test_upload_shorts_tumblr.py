import importlib.util
import json
import os
import stat
from pathlib import Path

import httpx
import pytest
from pydantic import SecretStr, ValidationError

from redblue.video import clients
from redblue.video import upload as up_yt
from redblue.video import upload_social as social
from redblue.video.models import Publication

_spec = importlib.util.spec_from_file_location("t_up", Path(__file__).with_name("test_upload.py"))
_yt = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_yt)


# ---------------------------------------------------------------------- YouTube Shorts


def test_as_short_tags_and_checks_the_render(tmp_path, monkeypatch):
    f = tmp_path / "v.mp4"
    f.write_bytes(b"x")
    monkeypatch.setattr("redblue.video.render.media_duration", lambda p: 45.0)
    req = _yt._req(title="Heat pumps")
    assert up_yt.as_short(req, f).title == "Heat pumps #Shorts"
    assert up_yt.as_short(up_yt.as_short(req, f), f).title == "Heat pumps #Shorts"  # once
    long = _yt._req(title="x" * 95, description="about")
    short = up_yt.as_short(long, f)
    assert short.title == "x" * 95 and short.description == "#Shorts\n\nabout"
    with pytest.raises(ValueError, match="can't be a Short"):
        up_yt.as_short(_yt._req(format="16:9"), f)
    monkeypatch.setattr("redblue.video.render.media_duration", lambda p: 181.0)
    with pytest.raises(ValueError, match="up to 3 minutes"):
        up_yt.as_short(req, f)


def test_youtube_upload_as_short(platform, vsettings, tmp_path, monkeypatch):
    monkeypatch.setattr("redblue.video.render.media_duration", lambda p: 30.0)
    s, calls = _yt._settings(vsettings), []
    with platform.db.session() as db:
        p = _yt._project(db, s)
        pub = up_yt.upload(
            db,
            p,
            _yt._req(title="Heat pumps", shorts=True),
            s,
            confirmed_by="ed",
            confirmed=True,
            http=_yt._youtube(calls),
        )
        init = next(c for c in calls if c.method == "POST" and c.url.host == "www.googleapis.com")
        assert json.loads(init.content)["snippet"]["title"] == "Heat pumps #Shorts"
        assert pub.url == "https://www.youtube.com/watch?v=AbCdEfGhIjK"


def test_admin_offers_shorts_for_vertical_renders(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from redblue.core.config import Settings
    from redblue.platform.app import create_app
    from redblue.platform.seed import ensure_admin
    from redblue.video import config as vconfig

    monkeypatch.chdir(tmp_path)
    for k, v in {
        "RB_VIDEO_OUTPUT_DIR": str(tmp_path / "out"),
        "RB_VIDEO_UPLOAD_ENABLED": "true",
        "YOUTUBE_OAUTH_CLIENT_ID": "cid",
        "YOUTUBE_OAUTH_CLIENT_SECRET": "sec",
        "YOUTUBE_UPLOAD_REFRESH_TOKEN": "up-ref",
    }.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr("redblue.video.render.media_duration", lambda p: 30.0)
    vconfig.get_video_settings.cache_clear()
    calls = []
    mock = _yt._youtube(calls)
    monkeypatch.setattr(clients, "_client", lambda c: c or mock)
    app = create_app(
        Settings(
            database_url=f"sqlite:///{tmp_path}/t.db",
            env="test",
            storage_dir=tmp_path / "m",
            secret_key="k" * 48,
        )
    )
    with app.state.rb.db.session() as db:
        ensure_admin(db, "admin@example.com", "correct-horse-battery")
        pid = _yt._project(db, vconfig.get_video_settings()).id
    c = TestClient(app)
    c.get("/admin/login")
    tok = c.cookies["rb_csrf"]
    c.post(
        "/admin/login",
        data={"email": "admin@example.com", "password": "correct-horse-battery", "csrf_token": tok},
    )
    url = f"/admin/video/projects/{pid}"
    assert 'name="shorts" value="1" checked' in c.get(url).text
    r = c.post(
        f"{url}/upload",
        data={
            "csrf_token": tok,
            "format": "9:16",
            "title": "Heat",
            "made_for_kids": "no",
            "confirm": "1",
            "shorts": "1",
        },
    )
    assert "Uploaded to YouTube as private" in r.text
    init = next(c for c in calls if c.method == "POST" and c.url.host == "www.googleapis.com")
    assert json.loads(init.content)["snippet"]["title"] == "Heat #Shorts"
    vconfig.get_video_settings.cache_clear()


# ---------------------------------------------------------------------- Tumblr

VIDEO = b"\x00\x00\x00\x18ftypmp42 tumblr video"


def _settings(vsettings, tmp_path, **extra):
    return vsettings.model_copy(
        update={
            "upload_enabled": True,
            "tumblr_client_id": "tb-key",
            "tumblr_client_secret": SecretStr("tb-secret"),
            "tumblr_refresh_token": SecretStr("tb-refresh-0"),
            "tumblr_blog": "heatfan",
            "tumblr_token_file": tmp_path / "tumblr.token",
            **extra,
        }
    )


def _project(db, s, status="approved"):
    from redblue.video.models import VideoProject

    s.output_dir.mkdir(parents=True, exist_ok=True)
    f = s.output_dir / "video-1-9x16.mp4"
    f.write_bytes(VIDEO)
    p = VideoProject(
        topic="Heat pumps",
        status=status,
        renders={"9:16": str(f)},
        script={
            "title": "Heat pumps explained",
            "hook": "h",
            "cta": "c",
            "hashtags": ["energy", "#home"],
            "beats": [
                {"narration": "a b", "visual_query": "q", "seconds": 3},
                {"narration": "c d", "visual_query": "q", "seconds": 3},
            ],
        },
    )
    db.add(p)
    db.flush()
    return p


class FakeTumblr:
    def __init__(self):
        self.calls: list[httpx.Request] = []
        self.state = "draft"
        self.refreshes = 0

    def handler(self, req: httpx.Request) -> httpx.Response:
        self.calls.append(req)
        path = req.url.path
        assert req.headers["user-agent"].startswith("redblue-video")
        if path == "/v2/oauth2/token":
            assert f"refresh_token=tb-refresh-{self.refreshes}".encode() in req.content
            self.refreshes += 1
            return httpx.Response(
                200,
                json={"access_token": "tb-access", "refresh_token": f"tb-refresh-{self.refreshes}"},
            )
        assert req.headers["authorization"] == "Bearer tb-access"
        if path == "/v2/blog/heatfan/posts" and req.method == "POST":
            return httpx.Response(
                201, json={"meta": {"status": 201}, "response": {"id": "7400000000000000001"}}
            )
        if path == "/v2/blog/heatfan/posts/7400000000000000001":
            return httpx.Response(
                200,
                json={
                    "meta": {"status": 200},
                    "response": {"id": "7400000000000000001", "state": self.state},
                },
            )
        return httpx.Response(404, json={"errors": [{"detail": f"unexpected {path}"}]})

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self.handler))

    def post_parts(self) -> tuple[dict, bytes]:
        req = next(r for r in self.calls if r.method == "POST" and r.url.path.endswith("/posts"))
        body = req.content
        assert body.index(b'name="json"') < body.index(b'name="video"')  # JSON part first
        start = body.index(b"{", body.index(b'name="json"'))
        end = body.index(b"\r\n--", start)
        return json.loads(body[start:end]), body


def test_tumblr_draft_then_published_elsewhere(platform, vsettings, tmp_path):
    s, fake = _settings(vsettings, tmp_path), FakeTumblr()
    with platform.db.session() as db:
        p = _project(db, s)
        d = social.defaults(p)
        req = social.TumblrRequest(caption=d["tumblr_caption"], tags=d["tumblr_tags"])
        up = social.upload_tumblr(
            db, p, req, s, confirmed_by="ed", confirmed=True, http=fake.client()
        )
        assert (up.status, up.mode, up.external_ref) == ("draft", "draft", "7400000000000000001")
        body, raw = fake.post_parts()
        assert body["state"] == "draft" and body["tags"] == "energy,home"
        assert body["content"][0] == {"type": "text", "text": d["tumblr_caption"]}
        assert body["content"][1] == {
            "type": "video",
            "media": {"type": "video/mp4", "identifier": "video", "width": 1080, "height": 1920},
        }
        assert VIDEO in raw
        token = tmp_path / "tumblr.token"
        assert token.read_text() == "tb-refresh-1"
        assert stat.S_IMODE(os.stat(token).st_mode) == 0o600
        assert db.query(Publication).count() == 0

        social.refresh(db, up, s, fake.client())  # still a draft
        assert up.status == "draft"
        fake.state = "published"  # the person publishes it from Tumblr
        social.refresh(db, up, s, fake.client())
        assert up.status == "published"
        assert up.url == "https://tumblr.com/heatfan/7400000000000000001"
        assert db.query(Publication).filter_by(platform="tumblr").count() == 1
        with pytest.raises(ValueError, match="already sent"):
            social.upload_tumblr(
                db, p, req, s, confirmed_by="ed", confirmed=True, http=fake.client()
            )


def test_tumblr_publish_now_and_private(platform, vsettings, tmp_path):
    s, fake = _settings(vsettings, tmp_path), FakeTumblr()
    with platform.db.session() as db:
        p = _project(db, s)
        with pytest.raises(ValueError, match="public on Tumblr"):
            social.upload_tumblr(
                db,
                p,
                social.TumblrRequest(state="published"),
                s,
                confirmed_by="ed",
                confirmed=True,
                http=fake.client(),
            )
        assert fake.calls == []
        up = social.upload_tumblr(
            db,
            p,
            social.TumblrRequest(state="published"),
            s,
            confirmed_by="ed",
            confirmed=True,
            confirmed_public=True,
            http=fake.client(),
        )
        assert up.status == "published" and up.published_by == "ed"
        assert up.url == "https://tumblr.com/heatfan/7400000000000000001"
    with pytest.raises(ValidationError):
        social.TumblrRequest(state="queue")


def test_tumblr_private_post_is_not_tracked(platform, vsettings, tmp_path):
    s, fake = _settings(vsettings, tmp_path), FakeTumblr()
    with platform.db.session() as db:
        up = social.upload_tumblr(
            db,
            _project(db, s),
            social.TumblrRequest(state="private"),
            s,
            confirmed_by="ed",
            confirmed=True,
            http=fake.client(),
        )
        assert up.privacy == "private" and up.url is None
        assert db.query(Publication).count() == 0


@pytest.mark.parametrize(
    "change, status, kwargs, error",
    [
        ({"upload_enabled": False}, "approved", {}, social.UploadDisabled),
        ({"tumblr_blog": ""}, "approved", {}, social.UploadDisabled),
        ({}, "review", {}, ValueError),
        ({}, "approved", {"confirmed": False}, ValueError),
    ],
)
def test_tumblr_guards_send_nothing(platform, vsettings, tmp_path, change, status, kwargs, error):
    s, fake = _settings(vsettings, tmp_path, **change), FakeTumblr()
    args = {"confirmed_by": "ed", "confirmed": True, **kwargs, "http": fake.client()}
    with platform.db.session() as db:
        with pytest.raises(error):
            social.upload_tumblr(
                db, _project(db, s, status=status), social.TumblrRequest(), s, **args
            )
    assert fake.calls == []


def test_tumblr_cli_needs_a_person(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from redblue.cli.main import app

    monkeypatch.setenv("RB_DATABASE_URL", f"sqlite:///{tmp_path}/c.db")
    r = CliRunner().invoke(app, ["video", "tumblr", "1"], input="y\n")
    assert r.exit_code != 0 and "person at the keyboard" in r.output


def test_admin_tumblr_flow(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from redblue.core.config import Settings
    from redblue.platform.app import create_app
    from redblue.platform.seed import ensure_admin
    from redblue.video import config as vconfig

    monkeypatch.chdir(tmp_path)
    for k, v in {
        "RB_VIDEO_OUTPUT_DIR": str(tmp_path / "out"),
        "RB_VIDEO_UPLOAD_ENABLED": "true",
        "TUMBLR_CLIENT_ID": "tb-key",
        "TUMBLR_CLIENT_SECRET": "tb-secret",
        "TUMBLR_REFRESH_TOKEN": "tb-refresh-0",
        "RB_VIDEO_TUMBLR_BLOG": "heatfan",
        "RB_VIDEO_TUMBLR_TOKEN_FILE": str(tmp_path / "tumblr.token"),
    }.items():
        monkeypatch.setenv(k, v)
    vconfig.get_video_settings.cache_clear()
    fake = FakeTumblr()
    mock = fake.client()
    monkeypatch.setattr(clients, "_client", lambda c: c or mock)
    app = create_app(
        Settings(
            database_url=f"sqlite:///{tmp_path}/t.db",
            env="test",
            storage_dir=tmp_path / "m",
            secret_key="k" * 48,
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
    url = f"/admin/video/projects/{pid}"
    page = c.get(url).text
    assert "Upload to Tumblr" in page and 'name="state" value="draft" checked' in page
    r = c.post(f"{url}/tumblr", data={"csrf_token": tok, "state": "published", "confirm": "1"})
    assert "public on Tumblr" in r.text and fake.calls == []
    r = c.post(f"{url}/tumblr", data={"csrf_token": tok, "confirm": "1", "tags": "a, #b"})
    assert "Saved as a Tumblr draft" in r.text and "publish it from your Tumblr drafts" in r.text
    vconfig.get_video_settings.cache_clear()
