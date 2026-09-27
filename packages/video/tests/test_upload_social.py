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

VIDEO = b"\x00\x00\x00\x18ftypmp42 fake mp4 bytes for social"


def _settings(vsettings, **extra):
    return vsettings.model_copy(
        update={
            "upload_enabled": True,
            "tiktok_client_key": "tk-key",
            "tiktok_client_secret": SecretStr("tk-secret"),
            "tiktok_refresh_token": SecretStr("tk-refresh"),
            "instagram_access_token": SecretStr("ig-token"),
            "instagram_user_id": "1784",
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


class FakePlatforms:
    """TikTok + Instagram APIs with scripted statuses; records every request."""

    def __init__(
        self,
        *,
        tiktok_status="PROCESSING_UPLOAD",
        ig_status="IN_PROGRESS",
        upload_host="open-upload.tiktokapis.com",
        rotate=False,
        privacy=None,
    ):
        self.calls: list[httpx.Request] = []
        self.tiktok_status, self.ig_status = tiktok_status, ig_status
        self.upload_host, self.rotate = upload_host, rotate
        self.privacy = privacy or ["SELF_ONLY", "FOLLOWER_OF_CREATOR", "PUBLIC_TO_EVERYONE"]

    def paths(self) -> list[str]:
        return [f"{r.method} {r.url.host}{r.url.path}" for r in self.calls]

    def handler(self, req: httpx.Request) -> httpx.Response:
        self.calls.append(req)
        host, path = req.url.host, req.url.path
        ok = {"error": {"code": "ok", "message": ""}}
        if host == "open.tiktokapis.com" and path == "/v2/oauth/token/":
            assert b"grant_type=refresh_token" in req.content
            body = {"access_token": "tk-access", "expires_in": 86400}
            if self.rotate:
                body["refresh_token"] = "tk-refresh-2"
            return httpx.Response(200, json=body)
        if host == "open.tiktokapis.com":
            assert req.headers["authorization"] == "Bearer tk-access"
            if path.endswith("/creator_info/query/"):
                return httpx.Response(
                    200,
                    json={
                        **ok,
                        "data": {
                            "creator_nickname": "Heat Pump Fan",
                            "creator_username": "heatfan",
                            "privacy_level_options": self.privacy,
                            "comment_disabled": True,
                            "duet_disabled": False,
                            "stitch_disabled": False,
                            "max_video_post_duration_sec": 600,
                        },
                    },
                )
            if path.endswith("/video/init/"):
                return httpx.Response(
                    200,
                    json={
                        **ok,
                        "data": {
                            "publish_id": "v_pub~1",
                            "upload_url": f"https://{self.upload_host}/video/?upload_id=1",
                        },
                    },
                )
            if path.endswith("/status/fetch/"):
                data = {"status": self.tiktok_status}
                if self.tiktok_status == "PUBLISH_COMPLETE":
                    data["publicaly_available_post_id"] = [7300000000000000001]
                if self.tiktok_status == "FAILED":
                    data["fail_reason"] = "file_format_check_failed"
                return httpx.Response(200, json={**ok, "data": data})
        if host == "open-upload.tiktokapis.com" and req.method == "PUT":
            assert req.headers["content-range"] == f"bytes 0-{len(VIDEO) - 1}/{len(VIDEO)}"
            assert req.content == VIDEO
            return httpx.Response(201)
        if host == "graph.facebook.com":
            assert req.headers["authorization"] == "Bearer ig-token"
            assert "access_token" not in str(req.url)  # token stays out of URLs and logs
            if path == "/v25.0/1784/media":
                return httpx.Response(200, json={"id": "17900001"})
            if path == "/v25.0/17900001":
                return httpx.Response(200, json={"status_code": self.ig_status, "status": "x"})
            if path == "/v25.0/1784/media_publish":
                return httpx.Response(200, json={"id": "18000002"})
            if path == "/v25.0/18000002":
                return httpx.Response(
                    200, json={"permalink": "https://www.instagram.com/reel/AbC123/"}
                )
        if host == "rupload.facebook.com":
            assert path == "/ig-api-upload/v25.0/17900001"
            assert req.headers["authorization"] == "OAuth ig-token"
            assert req.headers["offset"] == "0" and req.headers["file_size"] == str(len(VIDEO))
            return httpx.Response(200, json={"success": True})
        return httpx.Response(404, json={"error": {"message": f"unexpected {path}"}})

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self.handler))


def test_chunk_rules():
    assert clients.TikTok.chunks(3_000_000) == (3_000_000, 1)  # under 5 MB: one chunk
    assert clients.TikTok.chunks(64 * 2**20) == (64 * 2**20, 1)
    size, count = clients.TikTok.chunks(95 * 2**20)
    assert (size, count) == (10 * 2**20, 9)  # last chunk carries the remainder (15 MB)


def test_tiktok_inbox_then_status_then_published(platform, vsettings):
    s, fake = _settings(vsettings, tiktok_username="heatfan"), FakePlatforms()
    with platform.db.session() as db:
        p = _project(db, s)
        up = social.upload_tiktok(
            db, p, social.TikTokRequest(), s, confirmed_by="ed", confirmed=True, http=fake.client()
        )
        assert (up.mode, up.status, up.external_ref) == ("inbox", "processing", "v_pub~1")
        init = next(r for r in fake.calls if r.url.path.endswith("/inbox/video/init/"))
        body = json.loads(init.content)
        assert "post_info" not in body  # drafts: the person finishes in the TikTok app
        assert body["source_info"] == {
            "source": "FILE_UPLOAD",
            "video_size": len(VIDEO),
            "chunk_size": len(VIDEO),
            "total_chunk_count": 1,
        }
        fake.tiktok_status = "SEND_TO_USER_INBOX"
        assert social.refresh(db, up, s, fake.client()).status == "in_inbox"
        fake.tiktok_status = "PUBLISH_COMPLETE"
        social.refresh(db, up, s, fake.client())
        assert up.status == "published"
        assert up.url == "https://tiktok.com/@heatfan/video/7300000000000000001"
        assert db.query(Publication).filter_by(platform="tiktok").one().uploaded_by == "ed"
        with pytest.raises(ValueError, match="already sent"):
            social.upload_tiktok(
                db,
                p,
                social.TikTokRequest(),
                s,
                confirmed_by="ed",
                confirmed=True,
                http=fake.client(),
            )


def test_tiktok_failure_is_reported(platform, vsettings):
    s, fake = _settings(vsettings), FakePlatforms(tiktok_status="FAILED")
    with platform.db.session() as db:
        up = social.upload_tiktok(
            db,
            _project(db, s),
            social.TikTokRequest(),
            s,
            confirmed_by="ed",
            confirmed=True,
            http=fake.client(),
        )
        social.refresh(db, up, s, fake.client())
        assert up.status == "failed" and up.error == "file_format_check_failed"


def test_tiktok_direct_needs_a_chosen_allowed_privacy(platform, vsettings):
    s = _settings(vsettings, tiktok_mode="direct")
    fake = FakePlatforms(privacy=["SELF_ONLY", "FOLLOWER_OF_CREATOR"])
    kw = {"confirmed_by": "ed", "confirmed": True}
    with platform.db.session() as db:
        p = _project(db, s)
        opts = social.tiktok_options(s, fake.client())
        assert opts["nickname"] == "Heat Pump Fan" and opts["comment_disabled"]
        with pytest.raises(ValueError, match="Choose who can view"):
            social.upload_tiktok(db, p, social.TikTokRequest(), s, http=fake.client(), **kw)
        with pytest.raises(ValueError, match="isn't available"):
            social.upload_tiktok(
                db,
                p,
                social.TikTokRequest(privacy="PUBLIC_TO_EVERYONE"),
                s,
                http=fake.client(),
                **kw,
            )
        with pytest.raises(ValueError, match="other people"):
            social.upload_tiktok(
                db,
                p,
                social.TikTokRequest(privacy="FOLLOWER_OF_CREATOR"),
                s,
                http=fake.client(),
                **kw,
            )
        assert not any("/video/init/" in c for c in fake.paths())  # nothing sent yet
        req = social.TikTokRequest(privacy="SELF_ONLY", allow_comments=True, allow_duet=True)
        up = social.upload_tiktok(db, p, req, s, http=fake.client(), **kw)
        assert up.mode == "direct" and up.privacy == "SELF_ONLY"
        post = json.loads(
            next(r for r in fake.calls if r.url.path == "/v2/post/publish/video/init/").content
        )["post_info"]
        assert post["privacy_level"] == "SELF_ONLY" and post["is_aigc"] is True
        assert post["disable_comment"] is True  # the account has comments off
        assert post["disable_duet"] is False and post["disable_stitch"] is True
    with pytest.raises(ValidationError, match="Branded content"):
        social.TikTokRequest(privacy="SELF_ONLY", brand_content=True)


def test_tiktok_rejects_unexpected_upload_host_and_rotates_tokens(platform, vsettings, tmp_path):
    token_file = tmp_path / "tiktok.token"
    s = _settings(vsettings, tiktok_token_file=token_file)
    with platform.db.session() as db:
        p = _project(db, s)
        bad = FakePlatforms(upload_host="evil.example.com")
        with pytest.raises(ValueError, match="unexpected upload URL"):
            social.upload_tiktok(
                db,
                p,
                social.TikTokRequest(),
                s,
                confirmed_by="ed",
                confirmed=True,
                http=bad.client(),
            )
        assert not any(r.method == "PUT" for r in bad.calls)
        social.upload_tiktok(
            db,
            p,
            social.TikTokRequest(),
            s,
            confirmed_by="ed",
            confirmed=True,
            http=FakePlatforms(rotate=True).client(),
        )
    assert token_file.read_text() == "tk-refresh-2"
    assert stat.S_IMODE(os.stat(token_file).st_mode) == 0o600
    assert s.tiktok_credentials[2] == "tk-refresh-2"  # the file wins over the environment


def test_instagram_two_steps_and_only_a_person_publishes(platform, vsettings):
    s, fake = _settings(vsettings), FakePlatforms()
    with platform.db.session() as db:
        p = _project(db, s)
        req = social.InstagramRequest(caption=social.defaults(p)["instagram_caption"])
        up = social.upload_instagram(
            db, p, req, s, confirmed_by="ed", confirmed=True, http=fake.client()
        )
        assert (up.status, up.external_ref) == ("processing", "17900001")
        create = next(r for r in fake.calls if r.url.path == "/v25.0/1784/media")
        assert b"media_type=REELS" in create.content and b"upload_type=resumable" in create.content
        assert not any("media_publish" in c for c in fake.paths())

        # The tracking job checks status but never publishes, even when it's ready.
        fake.ig_status = "FINISHED"
        Pipeline(platform, s, fake.client()).track(db)
        assert up.status == "ready" and not any("media_publish" in c for c in fake.paths())

        with pytest.raises(ValueError, match="public on Instagram"):
            social.publish_instagram(
                db, up, s, confirmed_by="ed", confirmed=False, http=fake.client()
            )
        social.publish_instagram(
            db, up, s, confirmed_by="chief", confirmed=True, http=fake.client()
        )
        assert up.status == "published" and up.published_by == "chief"
        assert up.url == "https://instagram.com/reel/AbC123"  # normalized
        pub = db.query(Publication).filter_by(platform="instagram").one()
        assert pub.uploaded_by == "chief"
        with pytest.raises(ValueError, match="already published"):
            social.publish_instagram(
                db, up, s, confirmed_by="chief", confirmed=True, http=fake.client()
            )
        assert sum("media_publish" in c for c in fake.paths()) == 1


def test_instagram_not_ready_or_expired(platform, vsettings):
    s, fake = _settings(vsettings), FakePlatforms()
    with platform.db.session() as db:
        up = social.upload_instagram(
            db,
            _project(db, s),
            social.InstagramRequest(),
            s,
            confirmed_by="ed",
            confirmed=True,
            http=fake.client(),
        )
        with pytest.raises(ValueError, match="still processing"):
            social.publish_instagram(
                db, up, s, confirmed_by="ed", confirmed=True, http=fake.client()
            )
        fake.ig_status = "EXPIRED"
        with pytest.raises(ValueError, match="expired"):
            social.publish_instagram(
                db, up, s, confirmed_by="ed", confirmed=True, http=fake.client()
            )
        # An expired upload can be sent again.
        again = social.upload_instagram(
            db,
            db.get(VideoProject, up.project_id),
            social.InstagramRequest(),
            s,
            confirmed_by="ed",
            confirmed=True,
            http=fake.client(),
        )
        assert again.id != up.id
    with pytest.raises(ValidationError):
        social.InstagramRequest(caption="#a " * 31)


@pytest.mark.parametrize(
    "change, status, kwargs, error",
    [
        ({"upload_enabled": False}, "approved", {}, social.UploadDisabled),
        (
            {"tiktok_refresh_token": None, "instagram_access_token": None},
            "approved",
            {},
            social.UploadDisabled,
        ),
        ({}, "review", {}, ValueError),
        ({}, "approved", {"confirmed": False}, ValueError),
    ],
)
def test_guards_send_nothing(platform, vsettings, change, status, kwargs, error):
    s, fake = _settings(vsettings, **change), FakePlatforms()
    args = {"confirmed_by": "ed", "confirmed": True, **kwargs, "http": fake.client()}
    with platform.db.session() as db:
        p = _project(db, s, status=status)
        with pytest.raises(error):
            social.upload_tiktok(db, p, social.TikTokRequest(), s, **args)
        with pytest.raises(error):
            social.upload_instagram(db, p, social.InstagramRequest(), s, **args)
    assert fake.calls == []


@pytest.mark.parametrize("cmd", [["tiktok", "1"], ["instagram", "1"], ["instagram-publish", "1"]])
def test_cli_commands_need_a_person(tmp_path, monkeypatch, cmd):
    from typer.testing import CliRunner

    from redblue.cli.main import app

    monkeypatch.setenv("RB_DATABASE_URL", f"sqlite:///{tmp_path}/c.db")
    r = CliRunner().invoke(app, ["video", *cmd], input="y\n")
    assert r.exit_code != 0 and "person at the keyboard" in r.output


def test_admin_tiktok_and_instagram_flow(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from redblue.core.config import Settings
    from redblue.platform.app import create_app
    from redblue.platform.seed import ensure_admin
    from redblue.video import config as vconfig

    monkeypatch.chdir(tmp_path)
    for k, v in {
        "RB_VIDEO_OUTPUT_DIR": str(tmp_path / "out"),
        "RB_VIDEO_UPLOAD_ENABLED": "true",
        "TIKTOK_CLIENT_KEY": "tk-key",
        "TIKTOK_CLIENT_SECRET": "tk-secret",
        "TIKTOK_REFRESH_TOKEN": "tk-refresh",
        "INSTAGRAM_ACCESS_TOKEN": "ig-token",
        "INSTAGRAM_USER_ID": "1784",
    }.items():
        monkeypatch.setenv(k, v)
    vconfig.get_video_settings.cache_clear()
    fake = FakePlatforms()
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
    assert "Send to TikTok drafts" in page and "Upload Reel (not public yet)" in page

    r = c.post(f"{url}/tiktok", data={"csrf_token": tok, "format": "9:16"})
    assert "Confirm that you reviewed" in r.text and fake.calls == []
    r = c.post(f"{url}/tiktok", data={"csrf_token": tok, "format": "9:16", "confirm": "1"})
    assert "Open the TikTok app" in r.text

    r = c.post(f"{url}/instagram", data={"csrf_token": tok, "caption": "Hi", "confirm": "1"})
    assert "not public yet" in r.text
    with app.state.rb.db.session() as db:
        ig = db.query(Upload).filter_by(platform="instagram").one().id
    fake.ig_status = "FINISHED"
    r = c.post(f"/admin/video/uploads/{ig}/refresh", data={"csrf_token": tok})
    assert "Instagram upload: ready" in r.text and "Publish to Instagram" in r.text
    assert not any("media_publish" in p for p in fake.paths())
    assert c.post(f"/admin/video/uploads/{ig}/publish").status_code == 403  # CSRF
    r = c.post(f"/admin/video/uploads/{ig}/publish", data={"csrf_token": tok})
    assert "Confirm that this Reel" in r.text
    r = c.post(f"/admin/video/uploads/{ig}/publish", data={"csrf_token": tok, "confirm": "1"})
    assert "Published on Instagram: https://instagram.com/reel/AbC123" in r.text
    assert "published by admin@example.com" in c.get(url).text

    # Direct mode shows the account and its own privacy options, none preselected.
    monkeypatch.setenv("RB_VIDEO_TIKTOK_MODE", "direct")
    vconfig.get_video_settings.cache_clear()
    page = c.get(url).text
    assert "Posting as <strong>Heat Pump Fan</strong> (@heatfan)" in page
    assert '<option value="" selected disabled>Choose…</option>' in page
    assert '<option value="PUBLIC_TO_EVERYONE">Everyone</option>' in page
    assert 'name="allow_comments" value="1" disabled' in page  # the account has them off
    assert "Music Usage Confirmation" in page
    vconfig.get_video_settings.cache_clear()
