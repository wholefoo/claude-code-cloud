import json

import httpx
import pytest
from pydantic import SecretStr, ValidationError

from redblue.video import clients
from redblue.video import upload as up
from redblue.video.models import Publication, VideoProject

VIDEO = b"\x00\x00\x00\x18ftypmp42 fake mp4 bytes"


def _settings(vsettings, **extra):
    return vsettings.model_copy(
        update={
            "upload_enabled": True,
            "youtube_oauth_client_id": "cid",
            "youtube_oauth_client_secret": SecretStr("sec"),
            "youtube_upload_refresh_token": SecretStr("up-ref"),
            **extra,
        }
    )


def _youtube(
    calls: list, location="https://www.googleapis.com/upload/youtube/v3/videos?upload_id=u1"
):
    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(req)
        if req.url.host == "oauth2.googleapis.com":
            assert b"refresh_token=up-ref" in req.content
            return httpx.Response(200, json={"access_token": "up-token"})
        if req.method == "POST" and req.url.path == "/upload/youtube/v3/videos":
            assert req.headers["authorization"] == "Bearer up-token"
            assert req.url.params["uploadType"] == "resumable"
            assert req.headers["x-upload-content-length"] == str(len(VIDEO))
            return httpx.Response(200, headers={"location": location})
        if req.method == "PUT" and req.url.params.get("upload_id") == "u1":
            assert req.content == VIDEO
            return httpx.Response(
                200, json={"id": "AbCdEfGhIjK", "status": {"privacyStatus": "private"}}
            )
        return httpx.Response(404)

    return httpx.Client(transport=httpx.MockTransport(handler))


def _project(db, s, status="approved") -> VideoProject:
    s.output_dir.mkdir(parents=True, exist_ok=True)
    f = s.output_dir / "video-1-9x16.mp4"
    f.write_bytes(VIDEO)
    p = VideoProject(
        topic="Heat pumps",
        status=status,
        renders={"9:16": str(f)},
        script={
            "title": "Heat pumps <explained>",
            "hook": "h",
            "cta": "c",
            "hashtags": ["#energy", "home"],
            "beats": [
                {"narration": "a b", "visual_query": "q", "seconds": 3},
                {"narration": "c d", "visual_query": "q", "seconds": 3},
            ],
        },
        brief={"topic": "Heat pumps", "angle": "a", "audience": "b", "hooks": ["h"], "sources": []},
    )
    db.add(p)
    db.flush()
    return p


def _req(**kw) -> up.UploadRequest:
    return up.UploadRequest(**{"format": "9:16", "title": "T", "made_for_kids": False, **kw})


def test_upload_private_by_default_and_records_publication(platform, vsettings):
    s, calls = _settings(vsettings), []
    with platform.db.session() as db:
        p = _project(db, s)
        d = up.defaults(p, s)
        assert d["title"] == "Heat pumps ‹explained›" and d["tags"] == "energy, home"
        req = _req(title=d["title"], description=d["description"], tags=d["tags"])
        pub = up.upload(
            db, p, req, s, confirmed_by="ed@example.com", confirmed=True, http=_youtube(calls)
        )
        assert pub.url == "https://www.youtube.com/watch?v=AbCdEfGhIjK"
        assert (pub.platform, pub.format, pub.privacy) == ("youtube", "9:16", "private")
        assert pub.uploaded_by == "ed@example.com" and pub.external_id == "AbCdEfGhIjK"
        init = json.loads(
            next(
                c for c in calls if c.method == "POST" and c.url.host != "oauth2.googleapis.com"
            ).content
        )
        assert init["status"] == {
            "privacyStatus": "private",
            "selfDeclaredMadeForKids": False,
            "containsSyntheticMedia": True,
        }
        assert (
            init["snippet"]["tags"] == ["energy", "home"] and init["snippet"]["categoryId"] == "28"
        )
        with pytest.raises(ValueError, match="already uploaded"):
            up.upload(db, p, req, s, confirmed_by="ed", confirmed=True, http=_youtube([]))


@pytest.mark.parametrize(
    "change, kwargs, error",
    [
        ({"upload_enabled": False}, {}, up.UploadDisabled),
        ({"youtube_upload_refresh_token": None}, {}, up.UploadDisabled),
        ({}, {"confirmed": False}, ValueError),
        ({}, {"confirmed_by": " "}, ValueError),
    ],
)
def test_upload_refuses_without_switch_token_or_confirmation(
    platform, vsettings, change, kwargs, error
):
    s, calls = _settings(vsettings, **change), []
    with platform.db.session() as db:
        p = _project(db, s)
        args = {"confirmed_by": "ed", "confirmed": True, **kwargs}
        with pytest.raises(error):
            up.upload(db, p, _req(), s, http=_youtube(calls), **args)
    assert calls == []  # nothing left the building


def test_upload_needs_approval_public_confirmation_and_a_render(platform, vsettings):
    s, calls = _settings(vsettings), []
    with platform.db.session() as db:
        p = _project(db, s, status="review")
        with pytest.raises(ValueError, match="approved"):
            up.upload(db, p, _req(), s, confirmed_by="ed", confirmed=True, http=_youtube(calls))
        p.status = "approved"
        with pytest.raises(ValueError, match="public"):
            up.upload(
                db,
                p,
                _req(privacy="public"),
                s,
                confirmed_by="ed",
                confirmed=True,
                http=_youtube(calls),
            )
        with pytest.raises(ValueError, match="no 16:9 render"):
            up.upload(
                db,
                p,
                _req(format="16:9"),
                s,
                confirmed_by="ed",
                confirmed=True,
                http=_youtube(calls),
            )
        p.renders = {"9:16": "/etc/passwd"}
        with pytest.raises(ValueError, match="no 9:16 render"):
            up.upload(db, p, _req(), s, confirmed_by="ed", confirmed=True, http=_youtube(calls))
    assert calls == []


def test_upload_rejects_unexpected_session_url(platform, vsettings):
    s = _settings(vsettings)
    with platform.db.session() as db:
        p = _project(db, s)
        http = _youtube([], location="https://evil.example.com/steal")
        with pytest.raises(ValueError, match="unexpected upload URL"):
            up.upload(db, p, _req(), s, confirmed_by="ed", confirmed=True, http=http)
        assert db.query(Publication).count() == 0


def test_upload_request_validation():
    r = _req(title="<b>Hi</b>", tags="#a, a, b," + ",".join(f"t{i:03d}xxxxxxx" for i in range(60)))
    assert r.title == "‹b›Hi‹/b›" and r.tags[:2] == ["a", "b"]
    assert len(",".join(r.tags)) <= 450
    with pytest.raises(ValidationError):
        up.UploadRequest(format="9:16", title="T")  # made_for_kids has no default
    with pytest.raises(ValidationError):
        _req(title="x" * 101)
    with pytest.raises(ValidationError):
        _req(privacy="everyone")
    with pytest.raises(ValidationError):
        _req(description="é" * 2600)


def test_nothing_uploads_on_its_own(tmp_path, monkeypatch):
    """No job, schedule or pipeline step can upload."""
    from redblue.core.config import Settings
    from redblue.platform.app import create_app
    from redblue.video import pipeline

    app = create_app(
        Settings(database_url="sqlite://", env="test", storage_dir=tmp_path, secret_key="k" * 48)
    )
    jobs = app.state.rb.jobs
    video_jobs = [n for n in jobs.handlers if n.startswith("video.")]
    assert video_jobs and not [n for n in video_jobs if "upload" in n or "publish" in n]
    assert not [n for n, _, _ in jobs.schedules if "upload" in n]
    assert not [n for n in dir(pipeline.Pipeline) if "upload" in n or "publish" in n]


def test_cli_upload_needs_a_person(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from redblue.cli.main import app

    monkeypatch.setenv("RB_DATABASE_URL", f"sqlite:///{tmp_path}/c.db")
    r = CliRunner().invoke(app, ["video", "upload", "1", "--not-made-for-kids"], input="y\n")
    assert r.exit_code != 0 and "person at the keyboard" in r.output


def test_admin_upload_flow(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from redblue.core.config import Settings
    from redblue.platform.app import create_app
    from redblue.platform.seed import ensure_admin
    from redblue.video import config as vconfig

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("RB_VIDEO_OUTPUT_DIR", str(tmp_path / "out"))
    for k, v in {
        "YOUTUBE_OAUTH_CLIENT_ID": "cid",
        "YOUTUBE_OAUTH_CLIENT_SECRET": "sec",
        "YOUTUBE_UPLOAD_REFRESH_TOKEN": "up-ref",
    }.items():
        monkeypatch.setenv(k, v)
    vconfig.get_video_settings.cache_clear()
    calls = []
    mock = _youtube(calls)
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
    assert "Direct upload is off" in c.get(url).text  # needs RB_VIDEO_UPLOAD_ENABLED too

    monkeypatch.setenv("RB_VIDEO_UPLOAD_ENABLED", "true")
    vconfig.get_video_settings.cache_clear()
    page = c.get(url).text
    assert "Upload to YouTube" in page and 'value="private" checked' in page
    form = {"csrf_token": tok, "format": "9:16", "title": "Heat pumps", "privacy": "private"}
    r = c.post(f"{url}/upload", data={**form, "confirm": "1"})
    assert "Made for kids" in r.text
    r = c.post(f"{url}/upload", data={**form, "made_for_kids": "no"})
    assert "Confirm that you reviewed" in r.text
    r = c.post(
        f"{url}/upload", data={**form, "made_for_kids": "no", "confirm": "1", "privacy": "public"}
    )
    assert "should be public" in r.text
    assert calls == []
    no_token = {k: v for k, v in form.items() if k != "csrf_token"}
    r = c.post(f"{url}/upload", data={**no_token, "made_for_kids": "no", "confirm": "1"})
    assert r.status_code == 403 and calls == []  # CSRF enforced
    r = c.post(f"{url}/upload", data={**form, "made_for_kids": "no", "confirm": "1"})
    assert "Uploaded to YouTube as private" in r.text
    page = c.get(url).text
    assert "uploaded by admin@example.com" in page and "(uploaded)" in page
    r = c.post(f"{url}/upload", data={**form, "made_for_kids": "no", "confirm": "1"})
    assert "already uploaded" in r.text
    vconfig.get_video_settings.cache_clear()
