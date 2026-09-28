import json

import httpx
import pytest
from pydantic import SecretStr, ValidationError

from redblue.video import clients
from redblue.video import upload_social as social
from redblue.video.models import Publication, VideoProject
from redblue.video.performance import classify
from redblue.video.pipeline import Pipeline

VIDEO = b"\x00\x00\x00\x18ftypmp42 vimeo video bytes"


def _settings(vsettings, **extra):
    return vsettings.model_copy(
        update={"upload_enabled": True, "vimeo_access_token": SecretStr("vm-token"), **extra}
    )


def _project(db, s, status="approved"):
    s.output_dir.mkdir(parents=True, exist_ok=True)
    f = s.output_dir / "video-1-16x9.mp4"
    f.write_bytes(VIDEO)
    p = VideoProject(
        topic="Heat pumps",
        status=status,
        renders={"16:9": str(f)},
        script={
            "title": "Heat pumps explained",
            "hook": "h",
            "cta": "c",
            "beats": [
                {"narration": "a b", "visual_query": "q", "seconds": 3},
                {"narration": "c d", "visual_query": "q", "seconds": 3},
            ],
        },
    )
    db.add(p)
    db.flush()
    return p


class FakeVimeo:
    def __init__(self, upload_link="https://us-files.tus.vimeo.com/files/abc", short_first=False):
        self.calls: list[httpx.Request] = []
        self.transcode, self.view = "in_progress", "nobody"
        self.upload_link, self.short_first = upload_link, short_first

    def handler(self, req: httpx.Request) -> httpx.Response:
        self.calls.append(req)
        if req.url.host == "api.vimeo.com":
            assert req.headers["authorization"] == "Bearer vm-token"
            assert req.headers["accept"].startswith("application/vnd.vimeo.*+json")
            if req.method == "POST" and req.url.path == "/me/videos":
                body = json.loads(req.content)
                self.view = body["privacy"]["view"]
                assert body["upload"] == {"approach": "tus", "size": len(VIDEO)}
                return httpx.Response(
                    200,
                    json={
                        "uri": "/videos/900001",
                        "link": "https://vimeo.com/900001",
                        "upload": {"status": "in_progress", "upload_link": self.upload_link},
                    },
                )
            if req.url.path == "/videos/900001":
                return httpx.Response(
                    200,
                    json={
                        "transcode": {"status": self.transcode},
                        "privacy": {"view": self.view},
                        "link": "https://vimeo.com/900001",
                    },
                )
        if req.method == "PATCH" and req.url.host.endswith("vimeo.com"):
            assert "authorization" not in req.headers  # tus link is pre-authorized
            assert req.headers["tus-resumable"] == "1.0.0"
            offset = int(req.headers["upload-offset"])
            assert req.content == VIDEO[offset:]
            done = len(VIDEO) if not (self.short_first and offset == 0) else 10
            return httpx.Response(204, headers={"upload-offset": str(done)})
        return httpx.Response(404, json={"developer_message": f"unexpected {req.url}"})

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self.handler))


def test_only_me_by_default_then_made_public_on_vimeo(platform, vsettings):
    s, fake = _settings(vsettings), FakeVimeo(short_first=True)
    with platform.db.session() as db:
        p = _project(db, s)
        up = social.upload_vimeo(
            db,
            p,
            social.VimeoRequest(title="Heat pumps"),
            s,
            confirmed_by="ed",
            confirmed=True,
            http=fake.client(),
        )
        assert (up.status, up.privacy, up.external_ref) == ("processing", "nobody", "900001")
        offsets = [r.headers["upload-offset"] for r in fake.calls if r.method == "PATCH"]
        assert offsets == ["0", "10"]  # resumed from Vimeo's reported offset

        fake.transcode = "complete"
        Pipeline(platform, s, fake.client()).track(db)  # read-only status check
        assert up.status == "draft" and db.query(Publication).count() == 0
        fake.view = "anybody"  # the owner changes privacy on Vimeo
        social.refresh(db, up, s, fake.client())
        assert up.status == "published" and up.privacy == "anybody"
        assert up.url == "https://vimeo.com/900001"
        assert db.query(Publication).filter_by(platform="vimeo").count() == 1


def test_public_needs_second_confirmation(platform, vsettings):
    s, fake = _settings(vsettings), FakeVimeo()
    with platform.db.session() as db:
        p = _project(db, s)
        for privacy in ("anybody", "unlisted"):
            with pytest.raises(ValueError, match="other people"):
                social.upload_vimeo(
                    db,
                    p,
                    social.VimeoRequest(privacy=privacy),
                    s,
                    confirmed_by="ed",
                    confirmed=True,
                    http=fake.client(),
                )
        assert fake.calls == []
        up = social.upload_vimeo(
            db,
            p,
            social.VimeoRequest(privacy="anybody"),
            s,
            confirmed_by="ed",
            confirmed=True,
            confirmed_public=True,
            http=fake.client(),
        )
        assert up.published_by == "ed"
        fake.transcode = "complete"
        social.refresh(db, up, s, fake.client())
        assert up.status == "published"
    with pytest.raises(ValidationError):
        social.VimeoRequest(privacy="password")


def test_transcode_error_and_unexpected_upload_host(platform, vsettings):
    s = _settings(vsettings)
    with platform.db.session() as db:
        p = _project(db, s)
        bad = FakeVimeo(upload_link="https://evil.example.com/files/abc")
        with pytest.raises(ValueError, match="unexpected upload URL"):
            social.upload_vimeo(
                db,
                p,
                social.VimeoRequest(),
                s,
                confirmed_by="ed",
                confirmed=True,
                http=bad.client(),
            )
        assert not any(r.method == "PATCH" for r in bad.calls)
        fake = FakeVimeo()
        up = social.upload_vimeo(
            db, p, social.VimeoRequest(), s, confirmed_by="ed", confirmed=True, http=fake.client()
        )
        fake.transcode = "error"
        social.refresh(db, up, s, fake.client())
        assert up.status == "failed"


@pytest.mark.parametrize(
    "change, status, kwargs, error",
    [
        ({"upload_enabled": False}, "approved", {}, social.UploadDisabled),
        ({"vimeo_access_token": None}, "approved", {}, social.UploadDisabled),
        ({}, "review", {}, ValueError),
        ({}, "approved", {"confirmed": False}, ValueError),
    ],
)
def test_guards_send_nothing(platform, vsettings, change, status, kwargs, error):
    s, fake = _settings(vsettings, **change), FakeVimeo()
    args = {"confirmed_by": "ed", "confirmed": True, **kwargs, "http": fake.client()}
    with platform.db.session() as db:
        with pytest.raises(error):
            social.upload_vimeo(
                db, _project(db, s, status=status), social.VimeoRequest(), s, **args
            )
    assert fake.calls == []


def test_twitch_links_are_recognised_when_recorded_by_hand():
    assert classify("https://www.twitch.tv/videos/2100000001")[:2] == (
        "twitch",
        "https://twitch.tv/videos/2100000001",
    )


def test_vimeo_cli_needs_a_person(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from redblue.cli.main import app

    monkeypatch.setenv("RB_DATABASE_URL", f"sqlite:///{tmp_path}/c.db")
    r = CliRunner().invoke(app, ["video", "vimeo", "1"], input="y\n")
    assert r.exit_code != 0 and "person at the keyboard" in r.output


def test_admin_vimeo_flow(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from redblue.core.config import Settings
    from redblue.platform.app import create_app
    from redblue.platform.seed import ensure_admin
    from redblue.video import config as vconfig

    monkeypatch.chdir(tmp_path)
    for k, v in {
        "RB_VIDEO_OUTPUT_DIR": str(tmp_path / "out"),
        "RB_VIDEO_UPLOAD_ENABLED": "true",
        "VIMEO_ACCESS_TOKEN": "vm-token",
    }.items():
        monkeypatch.setenv(k, v)
    vconfig.get_video_settings.cache_clear()
    fake = FakeVimeo()
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
    assert "Upload to Vimeo" in page and 'name="privacy" value="nobody" checked' in page
    r = c.post(f"{url}/vimeo", data={"csrf_token": tok, "privacy": "anybody", "confirm": "1"})
    assert "Confirm that other people" in r.text and fake.calls == []
    r = c.post(f"{url}/vimeo", data={"csrf_token": tok, "confirm": "1", "title": "Heat"})
    assert "transcoding. Check status" in r.text
    from redblue.video.models import Upload

    with app.state.rb.db.session() as db:
        uid = db.query(Upload).filter_by(platform="vimeo").one().id
    fake.transcode = "complete"
    r = c.post(f"/admin/video/uploads/{uid}/refresh", data={"csrf_token": tok})
    assert "Vimeo upload: draft" in r.text and "change its privacy on Vimeo" in r.text
    vconfig.get_video_settings.cache_clear()


def test_only_configured_platforms_get_a_card(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from redblue.core.config import Settings
    from redblue.platform.app import create_app
    from redblue.platform.seed import ensure_admin
    from redblue.video import config as vconfig

    monkeypatch.chdir(tmp_path)
    for k, v in {
        "RB_VIDEO_OUTPUT_DIR": str(tmp_path / "out"),
        "RB_VIDEO_UPLOAD_ENABLED": "true",
        "VIMEO_ACCESS_TOKEN": "vm-token",
    }.items():
        monkeypatch.setenv(k, v)
    vconfig.get_video_settings.cache_clear()
    fake = FakeVimeo()
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
    # Vimeo is set up: its form sits in a collapsed card.
    assert '<details class="rb-card rb-upload" id="upload-vimeo">' in page
    assert f'action="/admin/video/projects/{pid}/vimeo"' in page
    # Everything else is one line each in "Other platforms", with the settings it needs.
    assert 'id="upload-tiktok"' not in page and '/tiktok"' not in page
    assert "Upload to TikTok" not in page and "Upload to YouTube" not in page
    assert "<h2>Other platforms</h2>" in page
    assert "TikTok: <code>TIKTOK_CLIENT_KEY</code>" in page
    assert "YouTube: <code>YOUTUBE_OAUTH_CLIENT_ID</code>" in page
    assert "Vimeo:" not in page.split("<h2>Other platforms</h2>")[1]
    assert "· sent" not in page

    # A failed upload comes back with its card open (and scrolled to), message intact.
    r = c.post(f"{url}/vimeo", data={"csrf_token": tok, "confirm": "1", "privacy": "anybody"})
    location = r.history[0].headers["location"]
    assert location.startswith(f"{url}?open=vimeo&msg=") and location.endswith("#upload-vimeo")
    assert '<details class="rb-card rb-upload" id="upload-vimeo" open>' in r.text
    assert "Confirm that other people" in r.text
    assert 'id="upload-vimeo" open' not in c.get(f"{url}?open=bogus").text

    r = c.post(f"{url}/vimeo", data={"csrf_token": tok, "confirm": "1", "title": "Heat"})
    assert "open=" not in r.history[0].headers["location"]  # success: cards stay closed
    page = c.get(url).text
    assert '<h2>Upload to Vimeo</h2> <span class="rb-muted">· sent</span>' in page

    # A platform without view counts shows its engagement, never "0 views".
    from redblue.video import performance

    with app.state.rb.db.session() as db:
        pub = performance.record(
            db, db.get(VideoProject, pid), "https://www.reddit.com/r/energy/comments/1abcde/x/"
        )
        performance.add_snapshot(db, pub, source="reddit_api", views=None, likes=321, comments=45)
    page = c.get(url).text
    assert "no view count · 321 points · 45 comments" in page and "0 views" not in page
    assert ">n/a</td>" in c.get("/admin/video/performance").text
    vconfig.get_video_settings.cache_clear()
