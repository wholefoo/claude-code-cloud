import httpx
import pytest
from pydantic import SecretStr, ValidationError

from redblue.video import clients
from redblue.video import upload_social as social
from redblue.video.models import Publication, VideoProject
from redblue.video.performance import classify
from redblue.video.pipeline import Pipeline

VIDEO = b"\x00\x00\x00\x18ftypmp42 dailymotion rumble bytes"


def _settings(vsettings, **extra):
    return vsettings.model_copy(
        update={
            "upload_enabled": True,
            "dailymotion_api_key": "dm-key",
            "dailymotion_api_secret": SecretStr("dm-secret"),
            "dailymotion_channel_id": "x2chan",
            "rumble_access_token": SecretStr("r" * 40),
            **extra,
        }
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


def _form(req: httpx.Request) -> dict:
    return dict(httpx.QueryParams(req.content.decode()))


class FakeDailymotion:
    def __init__(self, upload_url="https://upload-02.dc3.dailymotion.com/upload?uuid=abc"):
        self.calls: list[httpx.Request] = []
        self.created: dict = {}
        self.upload_url = upload_url
        self.status, self.published, self.private = "processing", False, False

    def handler(self, req: httpx.Request) -> httpx.Response:
        self.calls.append(req)
        if req.url.host == "partner.api.dailymotion.com":
            if req.url.path == "/oauth/v1/token":
                assert _form(req) == {
                    "grant_type": "client_credentials",
                    "client_id": "dm-key",
                    "client_secret": "dm-secret",
                    "scope": "manage_videos",
                }
                return httpx.Response(200, json={"access_token": "dm-at", "expires_in": 36000})
            assert req.headers["authorization"] == "Bearer dm-at"
            if req.url.path == "/rest/file/upload":
                return httpx.Response(200, json={"upload_url": self.upload_url})
            if req.method == "POST" and req.url.path == "/rest/user/x2chan/videos":
                self.created = _form(req)
                self.published = self.created["published"] == "true"
                self.private = self.created["private"] == "true"
                return httpx.Response(200, json={"id": "x9abcde"})
            if req.url.path == "/rest/video/x9abcde":
                return httpx.Response(
                    200,
                    json={
                        "id": "x9abcde",
                        "status": self.status,
                        "published": self.published,
                        "private": self.private,
                        "url": "https://www.dailymotion.com/video/x9abcde",
                    },
                )
        if req.method == "POST" and req.url.host.endswith("dailymotion.com"):
            assert "authorization" not in req.headers  # pre-signed upload URL
            assert VIDEO in req.content and b'name="file"' in req.content
            return httpx.Response(200, json={"url": "https://upload.dailymotion.com/f/abc"})
        return httpx.Response(404, json={"error": {"message": f"unexpected {req.url}"}})

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self.handler))


class FakeRumble:
    def __init__(self, body=None):
        self.calls: list[httpx.Request] = []
        self.body = body or {
            "success": True,
            "video_id": "v5abcd",
            "video_id_int": 123456,
            "url_monetized": "https://rumble.com/v5abcd-heat-pumps.html?mref=abc&mc=1",
        }

    def handler(self, req: httpx.Request) -> httpx.Response:
        self.calls.append(req)
        assert str(req.url) == "https://rumble.com/api/simple-upload.php"
        for field, value in (
            ("access_token", "r" * 40),
            ("title", "Heat pumps explained"),
            ("license_type", "0"),
            ("guid", "redblue-1-16x9"),
        ):
            assert f'name="{field}"\r\n\r\n{value}\r\n'.encode() in req.content
        assert b'name="video"' in req.content and VIDEO in req.content
        return httpx.Response(200, json=self.body)

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self.handler))


def test_dailymotion_draft_then_published_in_studio(platform, vsettings):
    s, fake = _settings(vsettings), FakeDailymotion()
    with platform.db.session() as db:
        p = _project(db, s)
        up = social.upload_dailymotion(
            db,
            p,
            social.DailymotionRequest(tags="heat, #energy, heat"),
            s,
            confirmed_by="ed",
            confirmed=True,
            http=fake.client(),
        )
        assert (up.status, up.privacy, up.external_ref) == ("processing", "nobody", "x9abcde")
        assert fake.created == {
            "url": "https://upload.dailymotion.com/f/abc",
            "title": "Heat pumps explained",
            "description": "",
            "tags": "heat,energy",
            "channel": "news",
            "published": "false",
            "private": "false",
            "is_created_for_kids": "false",
        }
        fake.status = "ready"
        Pipeline(platform, s, fake.client()).track(db)  # read-only status check
        assert up.status == "draft" and db.query(Publication).count() == 0
        fake.published, fake.status = True, "published"  # the owner publishes it
        social.refresh(db, up, s, fake.client())
        assert (up.status, up.privacy) == ("published", "public")
        assert up.url == "https://dailymotion.com/video/x9abcde"
        assert db.query(Publication).filter_by(platform="dailymotion").count() == 1


def test_dailymotion_private_or_public_needs_second_confirmation(platform, vsettings):
    s, fake = _settings(vsettings), FakeDailymotion()
    with platform.db.session() as db:
        p = _project(db, s)
        for visibility in ("private", "public"):
            with pytest.raises(ValueError, match="other people"):
                social.upload_dailymotion(
                    db,
                    p,
                    social.DailymotionRequest(visibility=visibility),
                    s,
                    confirmed_by="ed",
                    confirmed=True,
                    http=fake.client(),
                )
        assert fake.calls == []
        up = social.upload_dailymotion(
            db,
            p,
            social.DailymotionRequest(visibility="private", for_kids=True),
            s,
            confirmed_by="ed",
            confirmed=True,
            confirmed_public=True,
            http=fake.client(),
        )
        assert fake.created["private"] == "true" and fake.created["published"] == "true"
        assert fake.created["is_created_for_kids"] == "true" and up.published_by == "ed"
        fake.status = "published"
        social.refresh(db, up, s, fake.client())
        assert (up.status, up.privacy) == ("published", "unlisted")
    with pytest.raises(ValidationError):
        social.DailymotionRequest(visibility="password")


def test_dailymotion_encoding_error_and_unexpected_upload_host(platform, vsettings):
    s = _settings(vsettings)
    with platform.db.session() as db:
        p = _project(db, s)
        bad = FakeDailymotion(upload_url="https://evil.example.com/upload")
        with pytest.raises(ValueError, match="unexpected upload URL"):
            social.upload_dailymotion(
                db,
                p,
                social.DailymotionRequest(),
                s,
                confirmed_by="ed",
                confirmed=True,
                http=bad.client(),
            )
        assert not any(r.url.host == "evil.example.com" for r in bad.calls)
        fake = FakeDailymotion()
        up = social.upload_dailymotion(
            db,
            p,
            social.DailymotionRequest(),
            s,
            confirmed_by="ed",
            confirmed=True,
            http=fake.client(),
        )
        fake.status = "encoding_error"
        social.refresh(db, up, s, fake.client())
        assert up.status == "failed" and "encoding error" in up.error


def test_rumble_needs_second_confirmation_then_records_link(platform, vsettings):
    s, fake = _settings(vsettings), FakeRumble()
    with platform.db.session() as db:
        p = _project(db, s)
        with pytest.raises(ValueError, match="public on Rumble"):
            social.upload_rumble(
                db,
                p,
                social.RumbleRequest(),
                s,
                confirmed_by="ed",
                confirmed=True,
                http=fake.client(),
            )
        assert fake.calls == []
        up = social.upload_rumble(
            db,
            p,
            social.RumbleRequest(),
            s,
            confirmed_by="ed",
            confirmed=True,
            confirmed_public=True,
            http=fake.client(),
        )
        assert (up.status, up.external_ref, up.published_by) == ("published", "v5abcd", "ed")
        assert up.url == "https://rumble.com/v5abcd-heat-pumps.html"  # referral query dropped
        assert db.query(Publication).filter_by(platform="rumble").count() == 1
        with pytest.raises(ValueError, match="already sent"):
            social.upload_rumble(
                db,
                p,
                social.RumbleRequest(),
                s,
                confirmed_by="ed",
                confirmed=True,
                confirmed_public=True,
                http=fake.client(),
            )


def test_rumble_errors_are_shown(platform, vsettings):
    s = _settings(vsettings)
    fake = FakeRumble(body={"success": False, "errors": ["Invalid access token"]})
    with platform.db.session() as db:
        p = _project(db, s)
        with pytest.raises(clients.PlatformError, match="Invalid access token"):
            social.upload_rumble(
                db,
                p,
                social.RumbleRequest(),
                s,
                confirmed_by="ed",
                confirmed=True,
                confirmed_public=True,
                http=fake.client(),
            )
    with pytest.raises(ValidationError):
        social.RumbleRequest(license="exclusive")


@pytest.mark.parametrize("platform_name", ["dailymotion", "rumble"])
@pytest.mark.parametrize(
    "change, status, kwargs, error",
    [
        ({"upload_enabled": False}, "approved", {}, social.UploadDisabled),
        (
            {"dailymotion_channel_id": None, "rumble_access_token": None},
            "approved",
            {},
            social.UploadDisabled,
        ),
        ({}, "review", {}, ValueError),
        ({}, "approved", {"confirmed": False}, ValueError),
    ],
)
def test_guards_send_nothing(platform, vsettings, platform_name, change, status, kwargs, error):
    s = _settings(vsettings, **change)
    fake = FakeDailymotion() if platform_name == "dailymotion" else FakeRumble()
    args = {
        "confirmed_by": "ed",
        "confirmed": True,
        "confirmed_public": True,
        **kwargs,
        "http": fake.client(),
    }
    upload = getattr(social, f"upload_{platform_name}")
    req = social.DailymotionRequest() if platform_name == "dailymotion" else social.RumbleRequest()
    with platform.db.session() as db:
        with pytest.raises(error):
            upload(db, _project(db, s, status=status), req, s, **args)
    assert fake.calls == []


def test_links_are_recognised():
    assert classify("https://dai.ly/x9abcde")[0] == "dailymotion"
    assert classify("https://www.dailymotion.com/video/x9abcde")[:2] == (
        "dailymotion",
        "https://dailymotion.com/video/x9abcde",
    )
    assert classify("https://rumble.com/v5abcd-heat-pumps.html")[0] == "rumble"


@pytest.mark.parametrize("command", ["dailymotion", "rumble"])
def test_cli_needs_a_person(tmp_path, monkeypatch, command):
    from typer.testing import CliRunner

    from redblue.cli.main import app

    monkeypatch.setenv("RB_DATABASE_URL", f"sqlite:///{tmp_path}/c.db")
    r = CliRunner().invoke(app, ["video", command, "1"], input="y\ny\n")
    assert r.exit_code != 0 and "person at the keyboard" in r.output


def test_admin_dailymotion_and_rumble_flow(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from redblue.core.config import Settings
    from redblue.platform.app import create_app
    from redblue.platform.seed import ensure_admin
    from redblue.video import config as vconfig
    from redblue.video.models import Upload

    monkeypatch.chdir(tmp_path)
    for k, v in {
        "RB_VIDEO_OUTPUT_DIR": str(tmp_path / "out"),
        "RB_VIDEO_UPLOAD_ENABLED": "true",
        "DAILYMOTION_API_KEY": "dm-key",
        "DAILYMOTION_API_SECRET": "dm-secret",
        "DAILYMOTION_CHANNEL_ID": "x2chan",
        "RUMBLE_ACCESS_TOKEN": "r" * 40,
    }.items():
        monkeypatch.setenv(k, v)
    vconfig.get_video_settings.cache_clear()
    dm, rumble = FakeDailymotion(), FakeRumble()

    def route(req: httpx.Request) -> httpx.Response:
        return (rumble if req.url.host == "rumble.com" else dm).handler(req)

    mock = httpx.Client(transport=httpx.MockTransport(route))
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
    assert "Upload to Dailymotion" in page and 'name="visibility" value="draft" checked' in page
    assert "Upload to Rumble" in page and "Rumble has no drafts" in page

    r = c.post(
        f"{url}/dailymotion", data={"csrf_token": tok, "visibility": "public", "confirm": "1"}
    )
    assert "Confirm that other people" in r.text and dm.calls == []
    r = c.post(f"{url}/dailymotion", data={"csrf_token": tok, "confirm": "1"})
    assert "encoding. Check status" in r.text
    with app.state.rb.db.session() as db:
        uid = db.query(Upload).filter_by(platform="dailymotion").one().id
    dm.status = "ready"
    r = c.post(f"/admin/video/uploads/{uid}/refresh", data={"csrf_token": tok})
    assert "Dailymotion upload: draft" in r.text and "Dailymotion Studio" in r.text

    r = c.post(f"{url}/rumble", data={"csrf_token": tok, "confirm": "1"})
    assert "public on Rumble right away" in r.text and rumble.calls == []
    r = c.post(f"{url}/rumble", data={"csrf_token": tok, "confirm": "1", "confirm_public": "1"})
    assert "Published on Rumble: https://rumble.com/v5abcd-heat-pumps.html" in r.text
    vconfig.get_video_settings.cache_clear()
