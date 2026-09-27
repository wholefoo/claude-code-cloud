import importlib.util
import json
import time
from pathlib import Path

import httpx
import pytest
from pydantic import SecretStr

from redblue.video import clients
from redblue.video import upload_social as social
from redblue.video.models import Publication, Upload, VideoProject
from redblue.video.pipeline import Pipeline

_spec = importlib.util.spec_from_file_location("vconf", Path(__file__).with_name("conftest.py"))
_vconf = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_vconf)

S3 = "//reddit-uploaded-media.s3-accelerate.amazonaws.com"
BLOB = {"$type": "blob", "ref": {"$link": "bafkreihash"}, "mimeType": "video/mp4", "size": 1234}


def _settings(vsettings, **extra):
    return vsettings.model_copy(
        update={
            "upload_enabled": True,
            "reddit_username": "heatfan",
            "reddit_post_refresh_token": SecretStr("rd-post-refresh"),
            "bluesky_handle": "@heatfan.bsky.social",
            "bluesky_app_password": SecretStr("abcd-efgh-ijkl-mnop"),
            **extra,
        }
    )


def _project(db, s, video: bytes, status="approved") -> VideoProject:
    s.output_dir.mkdir(parents=True, exist_ok=True)
    f = s.output_dir / "video-1-9x16.mp4"
    f.write_bytes(video)
    p = VideoProject(
        topic="Heat pumps",
        status=status,
        renders={"9:16": str(f)},
        script={
            "title": "Heat pumps explained",
            "hook": "h",
            "cta": "c",
            "hashtags": ["energy", "home"],
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
        self.submitted: dict | None = None
        self.job_state = "JOB_STATE_ENCODING"
        self.s3_host = S3

    def paths(self) -> list[str]:
        return [f"{r.method} {r.url.host}{r.url.path}" for r in self.calls]

    def handler(self, req: httpx.Request) -> httpx.Response:
        self.calls.append(req)
        host, path = req.url.host, req.url.path
        # --- Reddit
        if host == "www.reddit.com" and path == "/api/v1/access_token":
            assert b"grant_type=refresh_token" in req.content
            assert b"refresh_token=rd-post-refresh" in req.content
            return httpx.Response(200, json={"access_token": "rd-user-token"})
        if host == "oauth.reddit.com":
            assert req.headers["authorization"] == "Bearer rd-user-token"
            assert req.headers["user-agent"].startswith("redblue-video")
            form = dict(httpx.QueryParams(req.content.decode())) if req.method == "POST" else {}
            if path == "/api/media/asset.json":
                kind = "videos" if form["mimetype"] == "video/mp4" else "images"
                return httpx.Response(
                    200,
                    json={
                        "args": {
                            "action": self.s3_host,
                            "fields": [
                                {"name": "key", "value": f"rte_{kind}/abc"},
                                {"name": "policy", "value": "p0l1cy"},
                            ],
                        },
                        "asset": {"asset_id": "abc"},
                    },
                )
            if path == "/api/submit":
                if form["sr"] == "nopromo":
                    return httpx.Response(
                        200,
                        json={
                            "json": {
                                "errors": [
                                    [
                                        "SUBREDDIT_NOTALLOWED",
                                        "you aren't allowed to post there.",
                                        "sr",
                                    ]
                                ]
                            }
                        },
                    )
                self.submitted = form
                return httpx.Response(
                    200,
                    json={
                        "json": {
                            "errors": [],
                            "data": {"websocket_url": "wss://ws-0.redditmedia.com/rte_images/abc"},
                        }
                    },
                )
            if path == "/user/heatfan/submitted":
                children = []
                if self.submitted:
                    children.append(
                        {
                            "data": {
                                "subreddit": self.submitted["sr"].lower(),
                                "title": self.submitted["title"],
                                "created_utc": time.time(),
                                "permalink": "/r/{}/comments/1abcde/heat_pumps/".format(
                                    self.submitted["sr"]
                                ),
                            }
                        }
                    )
                return httpx.Response(200, json={"data": {"children": children}})
        if host.endswith("amazonaws.com"):
            assert "authorization" not in req.headers
            assert b"p0l1cy" in req.content and b'name="file"' in req.content
            return httpx.Response(201)
        # --- Bluesky
        if host == "bsky.social" and path == "/xrpc/com.atproto.server.createSession":
            body = json.loads(req.content)
            assert body == {"identifier": "heatfan.bsky.social", "password": "abcd-efgh-ijkl-mnop"}
            return httpx.Response(
                200,
                json={
                    "accessJwt": "bs-access",
                    "did": "did:plc:abc123",
                    "handle": "heatfan.bsky.social",
                    "didDoc": {
                        "service": [
                            {
                                "id": "#atproto_pds",
                                "type": "AtprotoPersonalDataServer",
                                "serviceEndpoint": "https://morel.us-east.host.bsky.network",
                            }
                        ]
                    },
                },
            )
        if host == "bsky.social" and path == "/xrpc/com.atproto.server.getServiceAuth":
            assert req.headers["authorization"] == "Bearer bs-access"
            assert req.url.params["aud"] == "did:web:morel.us-east.host.bsky.network"
            assert req.url.params["lxm"] == "com.atproto.repo.uploadBlob"
            return httpx.Response(200, json={"token": "svc-token"})
        if host == "video.bsky.app" and path == "/xrpc/app.bsky.video.uploadVideo":
            assert req.headers["authorization"] == "Bearer svc-token"
            assert req.url.params["did"] == "did:plc:abc123"
            return httpx.Response(
                200, json={"jobId": "job-1", "did": "did:plc:abc123", "state": "JOB_STATE_CREATED"}
            )
        if host == "video.bsky.app" and path == "/xrpc/app.bsky.video.getJobStatus":
            status = {"jobId": "job-1", "state": self.job_state}
            if self.job_state == "JOB_STATE_COMPLETED":
                status["blob"] = BLOB
            if self.job_state == "JOB_STATE_FAILED":
                status["error"] = "Video too long"
            return httpx.Response(200, json={"jobStatus": status})
        if host == "bsky.social" and path == "/xrpc/com.atproto.repo.createRecord":
            assert req.headers["authorization"] == "Bearer bs-access"
            return httpx.Response(
                200, json={"uri": "at://did:plc:abc123/app.bsky.feed.post/3kxyzpost", "cid": "bafy"}
            )
        return httpx.Response(404, json={"message": f"unexpected {host}{path}"})

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self.handler))


def test_reddit_upload_then_person_posts_to_one_subreddit(platform, vsettings, media_files):
    s, fake = _settings(vsettings), Fake()
    with platform.db.session() as db:
        p = _project(db, s, media_files[0])
        up = social.upload_reddit(
            db, p, "9:16", s, confirmed_by="ed", confirmed=True, http=fake.client()
        )
        assert up.status == "ready" and not any(c.endswith("/api/submit") for c in fake.paths())
        assert up.meta["video_url"] == f"https:{S3}/rte_videos/abc"
        assert up.meta["poster_url"] == f"https:{S3}/rte_images/abc"
        poster = next(
            r
            for r in fake.calls
            if r.url.host.endswith("amazonaws.com") and b"image/jpeg" in r.content
        )
        assert b"\xff\xd8" in poster.content  # a real JPEG frame from the render

        # The tracking job never posts an upload that's waiting for a subreddit.
        Pipeline(platform, s, fake.client()).track(db)
        assert up.status == "ready" and not any(c.endswith("/api/submit") for c in fake.paths())

        kw = {"confirmed_by": "chief", "http": fake.client()}
        with pytest.raises(ValueError, match="one subreddit"):
            social.publish(
                db, up, s, subreddit="tech nology, gadgets", title="t", confirmed=True, **kw
            )
        with pytest.raises(ValueError, match="1 to 300"):
            social.publish(db, up, s, subreddit="technology", title=" ", confirmed=True, **kw)
        with pytest.raises(ValueError, match="rules"):
            social.publish(db, up, s, subreddit="technology", title="t", confirmed=False, **kw)
        with pytest.raises(clients.PlatformError, match="aren't allowed"):
            social.publish(db, up, s, subreddit="nopromo", title="t", confirmed=True, **kw)
        assert up.status == "ready"  # a refused post leaves it ready for another subreddit

        social.publish(
            db, up, s, subreddit="r/technology", title="Heat pumps explained", confirmed=True, **kw
        )
        assert fake.submitted["kind"] == "video" and fake.submitted["sr"] == "technology"
        assert fake.submitted["video_poster_url"] == f"https:{S3}/rte_images/abc"
        assert up.status == "published"
        assert up.url == "https://reddit.com/r/technology/comments/1abcde/heat_pumps"
        assert db.query(Publication).filter_by(platform="reddit").one().uploaded_by == "chief"
        with pytest.raises(ValueError, match="already posted"):
            social.publish(db, up, s, subreddit="gadgets", title="t", confirmed=True, **kw)
        assert sum(c.endswith("/api/submit") for c in fake.paths()) == 2  # refused + posted


def test_reddit_rejects_unexpected_upload_host(platform, vsettings, media_files):
    s, fake = _settings(vsettings), Fake()
    fake.s3_host = "//evil.example.com"
    with platform.db.session() as db:
        with pytest.raises(ValueError, match="unexpected upload URL"):
            social.upload_reddit(
                db,
                _project(db, s, media_files[0]),
                "9:16",
                s,
                confirmed_by="ed",
                confirmed=True,
                http=fake.client(),
            )
    assert not any("evil" in c for c in fake.paths())


def test_bluesky_upload_then_person_posts(platform, vsettings):
    s, fake = _settings(vsettings), Fake()
    with platform.db.session() as db:
        p = _project(db, s, b"video bytes")
        d = social.defaults(p)
        assert d["bluesky_text"] == "Heat pumps explained #energy #home (AI-assisted)"
        up = social.upload_bluesky(
            db,
            p,
            social.BlueskyRequest(text=d["bluesky_text"]),
            s,
            confirmed_by="ed",
            confirmed=True,
            http=fake.client(),
        )
        assert (up.status, up.external_ref) == ("processing", "job-1")

        fake.job_state = "JOB_STATE_COMPLETED"
        Pipeline(platform, s, fake.client()).track(db)
        assert up.status == "ready" and up.meta["blob"] == BLOB
        assert not any("createRecord" in c for c in fake.paths())

        with pytest.raises(ValueError, match="Confirm"):
            social.publish(db, up, s, confirmed_by="chief", confirmed=False, http=fake.client())
        social.publish(db, up, s, confirmed_by="chief", confirmed=True, http=fake.client())
        assert up.status == "published"
        assert up.url == "https://bsky.app/profile/heatfan.bsky.social/post/3kxyzpost"
        body = json.loads(next(r for r in fake.calls if "createRecord" in r.url.path).content)
        record = body["record"]
        assert body["repo"] == "did:plc:abc123" and body["collection"] == "app.bsky.feed.post"
        assert record["embed"] == {
            "$type": "app.bsky.embed.video",
            "video": BLOB,
            "aspectRatio": {"width": 1080, "height": 1920},
        }
        tags = [f["features"][0]["tag"] for f in record["facets"]]
        assert tags == ["energy", "home"]
        text = record["text"].encode()
        first = record["facets"][0]["index"]
        assert text[first["byteStart"] : first["byteEnd"]] == b"#energy"
        with pytest.raises(ValueError, match="already posted"):
            social.publish(db, up, s, confirmed_by="chief", confirmed=True, http=fake.client())


def test_bluesky_failed_processing(platform, vsettings):
    s, fake = _settings(vsettings), Fake()
    with platform.db.session() as db:
        up = social.upload_bluesky(
            db,
            _project(db, s, b"v"),
            social.BlueskyRequest(),
            s,
            confirmed_by="ed",
            confirmed=True,
            http=fake.client(),
        )
        fake.job_state = "JOB_STATE_FAILED"
        social.refresh(db, up, s, fake.client())
        assert up.status == "failed" and up.error == "Video too long"


def test_hashtag_facets_use_utf8_byte_offsets():
    text = "Wärme #pumpe, #2026 and #ok."
    facets = clients.hashtag_facets(text)
    raw = text.encode()
    assert [
        (f["features"][0]["tag"], raw[f["index"]["byteStart"] : f["index"]["byteEnd"]])
        for f in facets
    ] == [("pumpe", b"#pumpe"), ("ok", b"#ok")]


@pytest.mark.parametrize(
    "change, status, kwargs, error",
    [
        ({"upload_enabled": False}, "approved", {}, social.UploadDisabled),
        (
            {"reddit_post_refresh_token": None, "bluesky_app_password": None},
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
        p = _project(db, s, b"v", status=status)
        with pytest.raises(error):
            social.upload_reddit(db, p, "9:16", s, **args)
        with pytest.raises(error):
            social.upload_bluesky(db, p, social.BlueskyRequest(), s, **args)
    assert fake.calls == []


@pytest.mark.parametrize(
    "cmd",
    [
        ["reddit", "1"],
        ["reddit-publish", "1", "--subreddit", "technology", "--title", "t"],
        ["bluesky", "1"],
        ["bluesky-publish", "1"],
    ],
)
def test_cli_commands_need_a_person(tmp_path, monkeypatch, cmd):
    from typer.testing import CliRunner

    from redblue.cli.main import app

    monkeypatch.setenv("RB_DATABASE_URL", f"sqlite:///{tmp_path}/c.db")
    r = CliRunner().invoke(app, ["video", *cmd], input="y\n")
    assert r.exit_code != 0 and "person at the keyboard" in r.output


def test_admin_reddit_and_bluesky_flow(tmp_path, monkeypatch, media_files):
    from fastapi.testclient import TestClient

    from redblue.core.config import Settings
    from redblue.platform.app import create_app
    from redblue.platform.seed import ensure_admin
    from redblue.video import config as vconfig

    monkeypatch.chdir(tmp_path)
    for k, v in {
        "RB_VIDEO_OUTPUT_DIR": str(tmp_path / "out"),
        "RB_VIDEO_UPLOAD_ENABLED": "true",
        "REDDIT_CLIENT_ID": "rd-id",
        "REDDIT_CLIENT_SECRET": "rd-secret",
        "REDDIT_USERNAME": "heatfan",
        "REDDIT_POST_REFRESH_TOKEN": "rd-post-refresh",
        "BLUESKY_HANDLE": "heatfan.bsky.social",
        "BLUESKY_APP_PASSWORD": "abcd-efgh-ijkl-mnop",
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
        pid = _project(db, vconfig.get_video_settings(), media_files[0]).id
    c = TestClient(app)
    c.get("/admin/login")
    tok = c.cookies["rb_csrf"]
    c.post(
        "/admin/login",
        data={"email": "admin@example.com", "password": "correct-horse-battery", "csrf_token": tok},
    )
    url = f"/admin/video/projects/{pid}"
    page = c.get(url).text
    assert "Upload to Reddit" in page and "Upload to Bluesky" in page

    r = c.post(f"{url}/reddit", data={"csrf_token": tok, "confirm": "1"})
    assert "Choose a subreddit below" in r.text and "Post to Reddit" in r.text
    with app.state.rb.db.session() as db:
        rd = db.query(Upload).filter_by(platform="reddit").one().id
    r = c.post(
        f"/admin/video/uploads/{rd}/publish",
        data={"csrf_token": tok, "confirm": "1", "subreddit": "technology", "title": ""},
    )
    assert "1 to 300" in r.text
    r = c.post(
        f"/admin/video/uploads/{rd}/publish",
        data={
            "csrf_token": tok,
            "confirm": "1",
            "subreddit": "technology",
            "title": "Heat pumps explained",
        },
    )
    assert "Published on Reddit: https://reddit.com/r/technology/comments/1abcde" in r.text

    r = c.post(f"{url}/bluesky", data={"csrf_token": tok, "confirm": "1", "text": "Hi #heat"})
    assert "Uploaded to Bluesky" in r.text
    with app.state.rb.db.session() as db:
        bs = db.query(Upload).filter_by(platform="bluesky").one().id
    fake.job_state = "JOB_STATE_COMPLETED"
    r = c.post(f"/admin/video/uploads/{bs}/publish", data={"csrf_token": tok, "confirm": "1"})
    assert (
        "Published on Bluesky: https://bsky.app/profile/heatfan.bsky.social/post/3kxyzpost"
        in r.text
    )
    vconfig.get_video_settings.cache_clear()
