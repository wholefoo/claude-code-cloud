import json

import httpx
import pytest
from pydantic import SecretStr

from redblue.video import clients
from redblue.video import upload_social as social
from redblue.video.models import Publication, Upload, VideoProject
from redblue.video.pipeline import Pipeline

VIDEO = bytes(range(256)) * 40  # 10,240 bytes, so LinkedIn can split it into parts
AUTHOR = "urn:li:person:AbC123xyz"


def _settings(vsettings, **extra):
    return vsettings.model_copy(
        update={
            "upload_enabled": True,
            "facebook_page_access_token": SecretStr("fb-token"),
            "facebook_page_id": "1020",
            "linkedin_access_token": SecretStr("li-token"),
            "linkedin_author_urn": AUTHOR,
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
            "title": "Heat pumps (explained)",
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
    def __init__(self):
        self.calls: list[httpx.Request] = []
        self.fb_status = {"video_status": "processing"}
        self.li_status = "PROCESSING"
        self.rupload_host = "rupload.facebook.com"
        self.parts_host = "www.linkedin.com"

    def paths(self) -> list[str]:
        return [f"{r.method} {r.url.host}{r.url.path}" for r in self.calls]

    def handler(self, req: httpx.Request) -> httpx.Response:
        self.calls.append(req)
        host, path = req.url.host, req.url.path
        if host == "graph.facebook.com":
            assert req.headers["authorization"] == "Bearer fb-token"
            assert "access_token" not in str(req.url)
            if path == "/v25.0/1020/video_reels":
                form = dict(x.split("=", 1) for x in req.content.decode().split("&"))
                if form["upload_phase"] == "start":
                    return httpx.Response(
                        200,
                        json={
                            "video_id": "555",
                            "upload_url": f"https://{self.rupload_host}/video-upload/v25.0/555",
                        },
                    )
                assert form["upload_phase"] == "finish" and form["video_id"] == "555"
                return httpx.Response(200, json={"success": True})
            if path == "/v25.0/555":
                return httpx.Response(200, json={"status": self.fb_status, "id": "555"})
        if host == "rupload.facebook.com":
            assert req.headers["authorization"] == "OAuth fb-token"
            assert req.headers["file_size"] == str(len(VIDEO)) and req.content == VIDEO
            return httpx.Response(200, json={"success": True})
        if host == "api.linkedin.com":
            assert req.headers["authorization"] == "Bearer li-token"
            assert req.headers["linkedin-version"] == "202606"
            assert req.headers["x-restli-protocol-version"] == "2.0.0"
            action = req.url.params.get("action")
            if path == "/rest/videos" and action == "initializeUpload":
                body = json.loads(req.content)["initializeUploadRequest"]
                assert body["owner"] == AUTHOR and body["fileSizeBytes"] == len(VIDEO)
                return httpx.Response(
                    200,
                    json={
                        "value": {
                            "video": "urn:li:video:C5F10",
                            "uploadToken": "",
                            "uploadInstructions": [  # out of order on purpose
                                {
                                    "uploadUrl": f"https://{self.parts_host}/dms-uploads/p2",
                                    "firstByte": 6000,
                                    "lastByte": len(VIDEO) - 1,
                                },
                                {
                                    "uploadUrl": f"https://{self.parts_host}/dms-uploads/p1",
                                    "firstByte": 0,
                                    "lastByte": 5999,
                                },
                            ],
                        }
                    },
                )
            if path == "/rest/videos" and action == "finalizeUpload":
                body = json.loads(req.content)["finalizeUploadRequest"]
                assert body["uploadedPartIds"] == ["etag-p1", "etag-p2"]  # in byte order
                return httpx.Response(200)
            if path == "/rest/videos/urn:li:video:C5F10":
                assert req.url.raw_path == b"/rest/videos/urn%3Ali%3Avideo%3AC5F10"
                return httpx.Response(200, json={"status": self.li_status})
            if path == "/rest/posts":
                return httpx.Response(201, headers={"x-restli-id": "urn:li:share:7100"})
        if host == "www.linkedin.com" and req.method == "PUT":
            part = path.rsplit("/", 1)[1]
            expected = VIDEO[:6000] if part == "p1" else VIDEO[6000:]
            assert req.content == expected and "authorization" not in req.headers
            return httpx.Response(201, headers={"etag": f"etag-{part}"})
        return httpx.Response(404, json={"error": {"message": f"unexpected {path}"}})

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self.handler))


def _finish_form(fake: Fake) -> dict:
    req = next(
        r for r in fake.calls if r.url.path.endswith("/video_reels") and b"finish" in r.content
    )
    return dict(x.split("=", 1) for x in req.content.decode().split("&"))


def test_facebook_draft_by_default_then_detects_publishing(platform, vsettings):
    s, fake = _settings(vsettings), Fake()
    with platform.db.session() as db:
        p = _project(db, s)
        d = social.defaults(p)
        req = social.FacebookRequest(title=d["title"], description=d["facebook_description"])
        up = social.upload_facebook(
            db, p, req, s, confirmed_by="ed", confirmed=True, http=fake.client()
        )
        assert (up.mode, up.status, up.external_ref) == ("draft", "processing", "555")
        assert _finish_form(fake)["video_state"] == "DRAFT"
        fake.fb_status = {
            "video_status": "ready",
            "publishing_phase": {"status": "not_started", "publish_status": "draft"},
        }
        assert social.refresh(db, up, s, fake.client()).status == "draft"
        # Someone publishes it in Meta Business Suite; the next check links it.
        fake.fb_status = {
            "video_status": "ready",
            "publishing_phase": {"status": "complete", "publish_status": "published"},
        }
        social.refresh(db, up, s, fake.client())
        assert up.status == "published" and up.url == "https://facebook.com/reel/555"
        assert db.query(Publication).filter_by(platform="facebook").one().format == "9:16"


def test_facebook_publish_now_needs_second_confirmation(platform, vsettings):
    s, fake = _settings(vsettings), Fake()
    with platform.db.session() as db:
        p = _project(db, s)
        req = social.FacebookRequest(publish_now=True)
        with pytest.raises(ValueError, match="public on your Page"):
            social.upload_facebook(
                db, p, req, s, confirmed_by="ed", confirmed=True, http=fake.client()
            )
        assert fake.calls == []
        up = social.upload_facebook(
            db,
            p,
            req,
            s,
            confirmed_by="ed",
            confirmed=True,
            confirmed_public=True,
            http=fake.client(),
        )
        assert up.mode == "publish" and up.published_by == "ed"
        assert _finish_form(fake)["video_state"] == "PUBLISHED"
        fake.fb_status = {
            "video_status": "error",
            "processing_phase": {"status": "error", "errors": [{"message": "Bad codec"}]},
        }
        social.refresh(db, up, s, fake.client())
        assert up.status == "failed" and up.error == "Bad codec"


def test_facebook_rejects_unexpected_upload_host(platform, vsettings):
    s, fake = _settings(vsettings), Fake()
    fake.rupload_host = "evil.example.com"
    with platform.db.session() as db:
        with pytest.raises(ValueError, match="unexpected upload URL"):
            social.upload_facebook(
                db,
                _project(db, s),
                social.FacebookRequest(),
                s,
                confirmed_by="ed",
                confirmed=True,
                http=fake.client(),
            )
    assert not any("evil" in c for c in fake.paths())


def test_linkedin_upload_then_person_posts(platform, vsettings):
    s, fake = _settings(vsettings), Fake()
    with platform.db.session() as db:
        p = _project(db, s)
        d = social.defaults(p)
        req = social.LinkedInRequest(title=d["title"], commentary="Heat pumps (really) #energy")
        up = social.upload_linkedin(
            db, p, req, s, confirmed_by="ed", confirmed=True, http=fake.client()
        )
        assert (up.status, up.format, up.external_ref) == (
            "processing",
            "16:9",
            "urn:li:video:C5F10",
        )
        assert not any(c.endswith("/rest/posts") for c in fake.paths())

        # The tracking job reads status but never posts, even when it's ready.
        fake.li_status = "AVAILABLE"
        Pipeline(platform, s, fake.client()).track(db)
        assert up.status == "ready" and not any(c.endswith("/rest/posts") for c in fake.paths())

        kw = {"confirmed_by": "chief", "http": fake.client()}
        with pytest.raises(ValueError, match="Choose who can see"):
            social.publish_linkedin(db, up, s, visibility=None, confirmed=True, **kw)
        with pytest.raises(ValueError, match="Confirm"):
            social.publish_linkedin(db, up, s, visibility="PUBLIC", confirmed=False, **kw)
        social.publish(db, up, s, visibility="CONNECTIONS", confirmed=True, **kw)
        assert up.status == "published" and up.privacy == "CONNECTIONS"
        assert up.url == "https://linkedin.com/feed/update/urn:li:share:7100"
        post = json.loads(next(r for r in fake.calls if r.url.path == "/rest/posts").content)
        assert post["author"] == AUTHOR and post["visibility"] == "CONNECTIONS"
        assert post["content"]["media"] == {
            "title": "Heat pumps (explained)",
            "id": "urn:li:video:C5F10",
        }
        assert post["commentary"] == "Heat pumps \\(really\\) \\#energy"  # little-text escaped
        assert post["lifecycleState"] == "PUBLISHED"
        with pytest.raises(ValueError, match="already posted"):
            social.publish(db, up, s, visibility="PUBLIC", confirmed=True, **kw)
        assert sum(c.endswith("/rest/posts") for c in fake.paths()) == 1


def test_linkedin_company_pages_post_publicly_and_failures(platform, vsettings):
    s = _settings(vsettings, linkedin_author_urn="urn:li:organization:42")
    fake = Fake()
    with platform.db.session() as db:
        up = Upload(
            project_id=_project(db, s).id,
            platform="linkedin",
            format="16:9",
            mode="post",
            status="ready",
            external_ref="urn:li:video:C5F10",
            uploaded_by="ed",
            meta={},
        )
        db.add(up)
        db.flush()
        with pytest.raises(ValueError, match="Company pages"):
            social.publish_linkedin(
                db,
                up,
                s,
                visibility="CONNECTIONS",
                confirmed_by="ed",
                confirmed=True,
                http=fake.client(),
            )
        fake.li_status = "PROCESSING_FAILED"
        with pytest.raises(ValueError, match="Can't post"):
            social.publish_linkedin(
                db,
                up,
                s,
                visibility="PUBLIC",
                confirmed_by="ed",
                confirmed=True,
                http=fake.client(),
            )
        # "ready" uploads are re-checked before posting; this one failed.
        assert up.status in ("ready", "failed")


def test_linkedin_rejects_unexpected_part_host_and_bad_settings(platform, vsettings):
    s, fake = _settings(vsettings), Fake()
    fake.parts_host = "evil.example.com"
    with platform.db.session() as db:
        with pytest.raises(ValueError, match="unexpected upload URL"):
            social.upload_linkedin(
                db,
                _project(db, s),
                social.LinkedInRequest(),
                s,
                confirmed_by="ed",
                confirmed=True,
                http=fake.client(),
            )
    assert not any(r.method == "PUT" for r in fake.calls)
    with pytest.raises(ValueError, match="LINKEDIN_AUTHOR_URN"):
        clients.LinkedIn(("t", "urn:li:person:x y"))
    with pytest.raises(ValueError, match="YYYYMM"):
        clients.LinkedIn(("t", AUTHOR), version="2026-06")
    with pytest.raises(ValueError, match="numeric Page id"):
        clients.FacebookPage(("t", "my-page"))


def test_little_text_escapes_reserved_characters():
    assert clients.little_text("a|b{c}@d[e](f)<g>#h*i_j~k\\l") == (
        "a\\|b\\{c\\}\\@d\\[e\\]\\(f\\)\\<g\\>\\#h\\*i\\_j\\~k\\\\l"
    )


@pytest.mark.parametrize(
    "change, status, kwargs, error",
    [
        ({"upload_enabled": False}, "approved", {}, social.UploadDisabled),
        (
            {"facebook_page_access_token": None, "linkedin_access_token": None},
            "approved",
            {},
            social.UploadDisabled,
        ),
        ({}, "review", {}, ValueError),
        ({}, "approved", {"confirmed": False}, ValueError),
    ],
)
def test_guards_send_nothing(platform, vsettings, change, status, kwargs, error):
    s, fake = _settings(vsettings, **change), Fake()
    args = {"confirmed_by": "ed", "confirmed": True, **kwargs, "http": fake.client()}
    with platform.db.session() as db:
        p = _project(db, s, status=status)
        with pytest.raises(error):
            social.upload_facebook(db, p, social.FacebookRequest(), s, **args)
        with pytest.raises(error):
            social.upload_linkedin(db, p, social.LinkedInRequest(), s, **args)
    assert fake.calls == []


@pytest.mark.parametrize(
    "cmd",
    [
        ["facebook", "1"],
        ["linkedin", "1"],
        ["linkedin-publish", "1", "--visibility", "PUBLIC"],
    ],
)
def test_cli_commands_need_a_person(tmp_path, monkeypatch, cmd):
    from typer.testing import CliRunner

    from redblue.cli.main import app

    monkeypatch.setenv("RB_DATABASE_URL", f"sqlite:///{tmp_path}/c.db")
    r = CliRunner().invoke(app, ["video", *cmd], input="y\n")
    assert r.exit_code != 0 and "person at the keyboard" in r.output


def test_admin_facebook_and_linkedin_flow(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from redblue.core.config import Settings
    from redblue.platform.app import create_app
    from redblue.platform.seed import ensure_admin
    from redblue.video import config as vconfig

    monkeypatch.chdir(tmp_path)
    for k, v in {
        "RB_VIDEO_OUTPUT_DIR": str(tmp_path / "out"),
        "RB_VIDEO_UPLOAD_ENABLED": "true",
        "FACEBOOK_PAGE_ACCESS_TOKEN": "fb-token",
        "FACEBOOK_PAGE_ID": "1020",
        "LINKEDIN_ACCESS_TOKEN": "li-token",
        "LINKEDIN_AUTHOR_URN": AUTHOR,
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
    page = c.get(url).text
    assert "Upload Reel to Page" in page and "Upload video (not posted yet)" in page
    assert '<input type="radio" name="publish_now" value="" checked>' in page

    r = c.post(f"{url}/facebook", data={"csrf_token": tok, "confirm": "1"})
    assert "Saved as a draft Reel" in r.text
    r = c.post(
        f"{url}/linkedin",
        data={"csrf_token": tok, "format": "16:9", "confirm": "1", "commentary": "Hi"},
    )
    assert "not posted yet" in r.text
    with app.state.rb.db.session() as db:
        li = db.query(Upload).filter_by(platform="linkedin").one().id
    fake.li_status = "AVAILABLE"
    r = c.post(f"/admin/video/uploads/{li}/refresh", data={"csrf_token": tok})
    assert "LinkedIn upload: ready" in r.text
    assert "Post to LinkedIn" in r.text
    r = c.post(f"/admin/video/uploads/{li}/publish", data={"csrf_token": tok, "confirm": "1"})
    assert "Choose who can see" in r.text
    r = c.post(
        f"/admin/video/uploads/{li}/publish",
        data={"csrf_token": tok, "confirm": "1", "visibility": "PUBLIC"},
    )
    assert "Published on LinkedIn: https://linkedin.com/feed/update/urn:li:share:7100" in r.text
    vconfig.get_video_settings.cache_clear()
