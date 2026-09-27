import json
import os
import stat

import httpx
import pytest
from pydantic import SecretStr, ValidationError

from redblue.video import clients
from redblue.video import upload_social as social
from redblue.video.models import Publication, Upload, VideoProject
from redblue.video.pipeline import Pipeline

VIDEO = b"\x00\x00\x00\x18ftypmp42 pinterest video"
S3 = "https://pinterest-media-upload.s3-accelerate.amazonaws.com/"


def _settings(vsettings, tmp_path, **extra):
    return vsettings.model_copy(
        update={
            "upload_enabled": True,
            "pinterest_app_id": "pin-app",
            "pinterest_app_secret": SecretStr("pin-secret"),
            "pinterest_refresh_token": SecretStr("pin-refresh-0"),
            "pinterest_board_id": "5550001",
            "pinterest_token_file": tmp_path / "pinterest.token",
            **extra,
        }
    )


def _project(db, s, status="approved") -> VideoProject:
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
            "hashtags": ["energy"],
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
    def __init__(self, upload_url=S3):
        self.calls: list[httpx.Request] = []
        self.status = "registered"
        self.upload_url = upload_url

    def paths(self) -> list[str]:
        return [f"{r.method} {r.url.host}{r.url.path}" for r in self.calls]

    def handler(self, req: httpx.Request) -> httpx.Response:
        self.calls.append(req)
        host, path = req.url.host, req.url.path
        if host == "api.pinterest.com" and path == "/v5/oauth/token":
            assert req.headers["authorization"].startswith("Basic ")
            assert b"grant_type=refresh_token" in req.content
            return httpx.Response(
                200, json={"access_token": "pin-access", "refresh_token": "pin-refresh-1"}
            )
        if host == "api.pinterest.com":
            assert req.headers["authorization"] == "Bearer pin-access"
            if path == "/v5/media" and req.method == "POST":
                assert json.loads(req.content) == {"media_type": "video"}
                return httpx.Response(
                    201,
                    json={
                        "media_id": "12345",
                        "media_type": "video",
                        "upload_url": self.upload_url,
                        "upload_parameters": {
                            "key": "uploads/abc.mp4",
                            "policy": "p0l1cy",
                            "x-amz-signature": "sig",
                        },
                    },
                )
            if path == "/v5/media/12345":
                return httpx.Response(200, json={"media_id": "12345", "status": self.status})
            if path == "/v5/pins":
                return httpx.Response(201, json={"id": "987654321"})
        if host.endswith("amazonaws.com"):
            assert "authorization" not in req.headers  # no Pinterest token to storage
            body = req.content
            assert b'name="policy"' in body and b"p0l1cy" in body and VIDEO in body
            assert body.index(b'name="policy"') < body.index(b'name="file"')  # fields first
            return httpx.Response(204)
        return httpx.Response(404, json={"message": f"unexpected {path}"})

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self.handler))


def test_upload_then_person_pins(platform, vsettings, tmp_path):
    s, fake = _settings(vsettings, tmp_path), Fake()
    with platform.db.session() as db:
        p = _project(db, s)
        d = social.defaults(p)
        req = social.PinterestRequest(
            title=d["title"],
            description=d["pinterest_description"],
            alt_text="A heat pump outside a house",
            link="https://example.com/heat-pumps",
        )
        up = social.upload_pinterest(
            db, p, req, s, confirmed_by="ed", confirmed=True, http=fake.client()
        )
        assert (up.status, up.external_ref) == ("processing", "12345")
        assert not any(c.endswith("/v5/pins") for c in fake.paths())
        token = tmp_path / "pinterest.token"
        assert token.read_text() == "pin-refresh-1"
        assert stat.S_IMODE(os.stat(token).st_mode) == 0o600

        # The tracking job reads status but never pins, even when it's ready.
        fake.status = "succeeded"
        Pipeline(platform, s, fake.client()).track(db)
        assert up.status == "ready" and not any(c.endswith("/v5/pins") for c in fake.paths())

        with pytest.raises(ValueError, match="Confirm"):
            social.publish(db, up, s, confirmed_by="chief", confirmed=False, http=fake.client())
        social.publish(db, up, s, confirmed_by="chief", confirmed=True, http=fake.client())
        assert up.status == "published" and up.url == "https://pinterest.com/pin/987654321"
        pin = json.loads(next(r for r in fake.calls if r.url.path == "/v5/pins").content)
        assert pin == {
            "board_id": "5550001",
            "title": "Heat pumps explained",
            "description": d["pinterest_description"],
            "alt_text": "A heat pump outside a house",
            "link": "https://example.com/heat-pumps",
            "media_source": {
                "source_type": "video_id",
                "media_id": "12345",
                "cover_image_key_frame_time": 1,
            },
        }
        assert db.query(Publication).filter_by(platform="pinterest").one().uploaded_by == "chief"
        with pytest.raises(ValueError, match="already pinned"):
            social.publish(db, up, s, confirmed_by="chief", confirmed=True, http=fake.client())
        assert sum(c.endswith("/v5/pins") for c in fake.paths()) == 1


def test_failed_processing_and_unexpected_upload_host(platform, vsettings, tmp_path):
    s = _settings(vsettings, tmp_path)
    with platform.db.session() as db:
        p = _project(db, s)
        fake = Fake()
        up = social.upload_pinterest(
            db,
            p,
            social.PinterestRequest(),
            s,
            confirmed_by="ed",
            confirmed=True,
            http=fake.client(),
        )
        fake.status = "failed"
        social.refresh(db, up, s, fake.client())
        assert up.status == "failed"
        bad = Fake(upload_url="https://evil.example.com/upload")
        with pytest.raises(ValueError, match="unexpected upload URL"):
            social.upload_pinterest(
                db,
                p,
                social.PinterestRequest(),
                s,
                confirmed_by="ed",
                confirmed=True,
                http=bad.client(),
            )
        assert not any("evil" in c for c in bad.paths())


def test_request_and_settings_validation(vsettings, tmp_path):
    with pytest.raises(ValidationError, match="https"):
        social.PinterestRequest(link="javascript:alert(1)")
    with pytest.raises(ValidationError, match="https"):
        social.PinterestRequest(link="http://example.com")
    with pytest.raises(ValidationError):
        social.PinterestRequest(title="x" * 101)
    assert social.PinterestRequest(link="").link == ""
    assert not social.available(_settings(vsettings, tmp_path, pinterest_board_id=""))["pinterest"]
    with pytest.raises(ValueError, match="numeric board id"):
        clients.Pinterest(("a", "b", "c"), "my-board")
    (tmp_path / "pinterest.token").write_text("pin-refresh-9")
    assert _settings(vsettings, tmp_path).pinterest_credentials[2] == "pin-refresh-9"


@pytest.mark.parametrize(
    "change, status, kwargs, error",
    [
        ({"upload_enabled": False}, "approved", {}, social.UploadDisabled),
        (
            {"pinterest_refresh_token": None, "pinterest_token_file": None},
            "approved",
            {},
            social.UploadDisabled,
        ),
        ({}, "review", {}, ValueError),
        ({}, "approved", {"confirmed": False}, ValueError),
    ],
)
def test_guards_send_nothing(platform, vsettings, tmp_path, change, status, kwargs, error):
    s, fake = _settings(vsettings, tmp_path, **change), Fake()
    args = {"confirmed_by": "ed", "confirmed": True, **kwargs, "http": fake.client()}
    with platform.db.session() as db:
        with pytest.raises(error):
            social.upload_pinterest(
                db, _project(db, s, status=status), social.PinterestRequest(), s, **args
            )
    assert fake.calls == []


@pytest.mark.parametrize("cmd", [["pinterest", "1"], ["pinterest-publish", "1"]])
def test_cli_commands_need_a_person(tmp_path, monkeypatch, cmd):
    from typer.testing import CliRunner

    from redblue.cli.main import app

    monkeypatch.setenv("RB_DATABASE_URL", f"sqlite:///{tmp_path}/c.db")
    r = CliRunner().invoke(app, ["video", *cmd], input="y\n")
    assert r.exit_code != 0 and "person at the keyboard" in r.output


def test_admin_pinterest_flow(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from redblue.core.config import Settings
    from redblue.platform.app import create_app
    from redblue.platform.seed import ensure_admin
    from redblue.video import config as vconfig

    monkeypatch.chdir(tmp_path)
    for k, v in {
        "RB_VIDEO_OUTPUT_DIR": str(tmp_path / "out"),
        "RB_VIDEO_UPLOAD_ENABLED": "true",
        "PINTEREST_APP_ID": "pin-app",
        "PINTEREST_APP_SECRET": "pin-secret",
        "PINTEREST_REFRESH_TOKEN": "pin-refresh-0",
        "RB_VIDEO_PINTEREST_BOARD_ID": "5550001",
        "RB_VIDEO_PINTEREST_TOKEN_FILE": str(tmp_path / "pinterest.token"),
    }.items():
        monkeypatch.setenv(k, v)
    vconfig.get_video_settings.cache_clear()
    fake = Fake()
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
    assert "Upload video (not pinned yet)" in c.get(url).text
    r = c.post(
        f"{url}/pinterest", data={"csrf_token": tok, "confirm": "1", "link": "javascript:alert(1)"}
    )
    assert "must be an https://" in r.text and fake.calls == []
    r = c.post(f"{url}/pinterest", data={"csrf_token": tok, "confirm": "1", "title": "Heat"})
    assert "not pinned yet" in r.text
    with app.state.rb.db.session() as db:
        uid = db.query(Upload).filter_by(platform="pinterest").one().id
    fake.status = "succeeded"
    r = c.post(f"/admin/video/uploads/{uid}/refresh", data={"csrf_token": tok})
    assert "Pinterest upload: ready" in r.text and "Pin to Pinterest" in r.text
    r = c.post(f"/admin/video/uploads/{uid}/publish", data={"csrf_token": tok, "confirm": "1"})
    assert "Published on Pinterest: https://pinterest.com/pin/987654321" in r.text
    vconfig.get_video_settings.cache_clear()


def test_snapchat_links_are_recognised_when_recorded_by_hand():
    from redblue.video.performance import classify

    platform, url, _ = classify("https://www.snapchat.com/spotlight/W7_EDlXWTBiXAEEniNoMPwAA/")
    assert (
        platform == "snapchat" and url == "https://snapchat.com/spotlight/W7_EDlXWTBiXAEEniNoMPwAA"
    )
