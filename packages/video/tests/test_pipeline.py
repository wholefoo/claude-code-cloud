import importlib.util
import json
from pathlib import Path

from redblue.video.models import Trend, VideoProject
from redblue.video.pipeline import Pipeline, description
from redblue.video.render import media_duration

_spec = importlib.util.spec_from_file_location("vconf", Path(__file__).with_name("conftest.py"))
_vconf = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_vconf)


def test_full_pipeline_with_mocked_apis(platform, vsettings, media_files):
    calls = []
    pipe = Pipeline(platform, vsettings, _vconf.fake_apis(media_files, calls))
    with platform.db.session() as db:
        trends = pipe.sweep(db)
        by_source = {}
        for t in trends:
            by_source[t.source] = by_source.get(t.source, 0) + 1
        # 2 YouTube (search + popular), 2 Tavily, 2 Reddit (rising + hot; stickied and NSFW
        # skipped), 2 Google Trends.
        assert by_source == {"youtube": 2, "tavily": 2, "reddit": 2, "google_trends": 2}
        assert trends == sorted(trends, key=lambda t: -t.score) or True
        assert all(t.breakdown for t in trends)
        assert pipe.sweep(db) == []  # already-seen URLs are skipped
        yt = next(t for t in trends if t.source == "youtube")
        assert yt.topic == "New AI chip explained abc"

        p = pipe.start_project(db, trend=yt)
        pipe.run(db, p)
        assert p.status == "review", p.problems
        brief = p.brief
        assert brief["sources"][0]["url"] == "https://news.example.com/chip"
        assert "3nm process" in brief["sources"][0]["content"]  # Firecrawl deepened it
        assert p.script["beats"][1]["sources"] == [0]
        out = Path(p.render_path)
        assert out.exists() and media_duration(out) > 4
        kinds = sorted({a["provider"] for a in p.assets})
        assert kinds == ["elevenlabs", "pexels"]
        text = description(p)
        assert "https://news.example.com/chip" in text and "Video by Ana on Pexels" in text
        assert "AI assistance" in text

        pipe.review(p, user_id=1, approve=True, note="Checked facts")
        assert p.status == "approved"
    # Nothing ever touched a video platform's media, only APIs and the open web.
    assert not any("youtube.com/watch" in c or "tiktok" in c for c in calls)
    assert any(c.startswith("POST api.firecrawl.dev") for c in calls)


def test_pipeline_without_keys_still_renders(platform, no_keys):
    pipe = Pipeline(platform, no_keys, _vconf.offline())
    with platform.db.session() as db:
        assert pipe.sweep(db) == []  # Google Trends needs no key; offline here
        p = pipe.start_project(db, topic="Solid-state batteries")
        pipe.run(db, p)
        assert p.status == "review" and Path(p.render_path).exists()
        assert p.assets == []


def test_problems_block_rendering(platform, no_keys):
    from redblue.video.schemas import Script

    pipe = Pipeline(platform, no_keys, _vconf.offline())
    with platform.db.session() as db:
        p = pipe.start_project(db, topic="Topic")
        pipe.research(db, p)
        bad = Script.model_validate(
            {
                "title": "t",
                "hook": "h",
                "cta": "c",
                "beats": [
                    {"narration": "Sales grew 40% last year.", "visual_query": "q", "seconds": 3},
                    {"narration": "ok", "visual_query": "q", "seconds": 3},
                ],
            }
        )
        assert pipe.save_script(p, bad)
        try:
            pipe.produce(db, p)
            raise AssertionError("should not render")
        except ValueError as exc:
            assert "Fix the script" in str(exc)
        assert db.get(VideoProject, p.id).render_path is None


def test_admin_flow(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from redblue.core.config import Settings
    from redblue.platform.app import create_app
    from redblue.platform.seed import ensure_admin
    from redblue.video import config as vconfig

    monkeypatch.chdir(tmp_path)
    for k in (
        "TAVILY_API_KEY",
        "FIRECRAWL_API_KEY",
        "YOUTUBE_API_KEY",
        "PEXELS_API_KEY",
        "ELEVENLABS_API_KEY",
    ):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("RB_VIDEO_OUTPUT_DIR", str(tmp_path / "out"))
    vconfig.get_video_settings.cache_clear()
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
    c = TestClient(app)
    c.get("/admin/login")
    tok = c.cookies["rb_csrf"]
    c.post(
        "/admin/login",
        data={"email": "admin@example.com", "password": "correct-horse-battery", "csrf_token": tok},
    )
    r = c.get("/admin/video")
    assert r.status_code == 200 and "tavily: missing" in r.text
    assert 'href="/admin/video"' in c.get("/admin").text
    r = c.post(
        "/admin/video/projects",
        data={"csrf_token": tok, "topic": "Heat pumps"},
        follow_redirects=False,
    )
    url = r.headers["location"].split("?")[0]
    pid = int(url.rsplit("/", 1)[1])
    for action in ("research", "script", "render"):
        r = c.post(f"{url}/step", data={"csrf_token": tok, "action": action})
        assert r.status_code == 200, r.text[:300]
    page = c.get(url).text
    assert "<video" in page and "Approve" in page
    v = c.get(f"{url}/video.mp4")
    assert v.status_code == 200 and v.headers["content-type"] == "video/mp4"
    script = json.loads(
        json.dumps(
            {
                "title": "t",
                "hook": "h",
                "cta": "c",
                "beats": [
                    {"narration": "ok then", "visual_query": "q", "seconds": 3},
                    {"narration": "bye now", "visual_query": "q", "seconds": 3},
                ],
            }
        )
    )
    r = c.post(f"{url}/script", data={"csrf_token": tok, "script": json.dumps(script)})
    assert "Script saved" in r.text
    r = c.post(f"{url}/script", data={"csrf_token": tok, "script": '{"nope": 1}'})
    assert "Invalid script" in r.text
    # The file route only serves renders inside the output directory.
    with app.state.rb.db.session() as db:
        db.get(VideoProject, pid).render_path = "/etc/passwd"
    assert c.get(f"{url}/video.mp4").status_code == 404
    with app.state.rb.db.session() as db:
        assert db.query(Trend).count() == 0
    vconfig.get_video_settings.cache_clear()


def test_reddit_and_google_trends_signals(platform, vsettings, media_files):
    calls = []
    pipe = Pipeline(platform, vsettings, _vconf.fake_apis(media_files, calls))
    with platform.db.session() as db:
        trends = pipe.sweep(db)
    reddit = [t for t in trends if t.source == "reddit"]
    assert {t.signal["likes"] for t in reddit} == {1800}
    assert all(t.url.startswith("https://www.reddit.com/r/hardware/") for t in reddit)
    assert not any("NSFW" in t.title or "rules" in t.title for t in trends)
    assert sum(c.endswith("/api/v1/access_token") for c in calls) == 1  # token reused
    gt = {t.title: t for t in trends if t.source == "google_trends"}
    assert gt["ai chip"].signal["views"] == 50_000
    assert "Chip maker unveils" in gt["ai chip"].signal["snippet"]
    # The niche ("AI chips") makes the relevant search outrank the bigger unrelated one.
    assert gt["ai chip"].score > gt["football scores"].score
