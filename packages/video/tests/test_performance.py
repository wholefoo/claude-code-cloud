import importlib.util
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from pydantic import SecretStr

from redblue.core.db import utcnow
from redblue.video import performance as perf
from redblue.video.models import MetricSnapshot, Publication, Trend, VideoProject
from redblue.video.pipeline import Pipeline
from redblue.video.schemas import TrendSignal
from redblue.video.scoring import Feedback, score_all, weights

_spec = importlib.util.spec_from_file_location("vconf", Path(__file__).with_name("conftest.py"))
_vconf = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_vconf)


@pytest.mark.parametrize(
    "url",
    [
        "https://www.youtube.com/shorts/dQw4w9WgXcQ",
        "https://youtu.be/dQw4w9WgXcQ?si=abc",
        "https://m.youtube.com/watch?v=dQw4w9WgXcQ&t=3",
        "http://youtube.com/live/dQw4w9WgXcQ",
    ],
)
def test_youtube_urls_normalize_to_one_id(url):
    assert perf.classify(url) == (
        "youtube",
        "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        "dQw4w9WgXcQ",
    )


def test_classify_other_platforms_and_rejects_bad_urls():
    assert perf.classify("https://www.tiktok.com/@me/video/123/")[:2] == (
        "tiktok",
        "https://tiktok.com/@me/video/123",
    )
    assert perf.classify("https://example.com/v/1")[0] == "other"
    assert perf.classify("https://www.tiktok.com/@me/video/1", "other")[0] == "other"
    for bad, platform in [
        ("javascript:alert(1)", None),
        ("https://user:pw@youtube.com/watch?v=dQw4w9WgXcQ", None),
        ("https://youtube.com/channel/xyz", None),
        ("https://evil-youtube.com/watch?v=dQw4w9WgXcQ", "youtube"),
        ("https://tiktok.com/@me/video/1", "instagram"),
        ("https://x.com/a", "myspace"),
        ("", None),
    ]:
        with pytest.raises(ValueError):
            perf.classify(bad, platform)


def _pub(hours_ago: float = 100) -> Publication:
    return Publication(
        id=1,
        project_id=1,
        platform="youtube",
        url="u",
        published_at=utcnow() - timedelta(hours=hours_ago),
    )


def _snap(pub: Publication, hours: float, views: int) -> MetricSnapshot:
    return MetricSnapshot(
        publication_id=pub.id,
        taken_at=pub.published_at + timedelta(hours=hours),
        views=views,
        source="t",
    )


def test_views_at_interpolates_and_waits_for_maturity():
    pub = _pub()
    snaps = [_snap(pub, 48, 1000), _snap(pub, 96, 3000)]
    assert perf.views_at(pub, snaps, 72) == 2000
    assert perf.views_at(pub, snaps, 24) == 500  # between publish (0 views) and 48 h
    assert perf.views_at(pub, snaps[:1], 72) is None  # not mature yet
    assert perf.views_at(pub, [], 72) is None


def test_record_requires_approval_and_unique_urls(platform):
    with platform.db.session() as db:
        p = VideoProject(topic="t", status="review")
        db.add(p)
        db.flush()
        with pytest.raises(ValueError, match="Approve"):
            perf.record(db, p, "https://youtu.be/dQw4w9WgXcQ")
        p.status = "approved"
        pub = perf.record(db, p, "https://youtu.be/dQw4w9WgXcQ", format="9x16")
        assert pub.external_id == "dQw4w9WgXcQ" and pub.format == "9:16"
        with pytest.raises(ValueError, match="already"):
            perf.record(db, p, "https://www.youtube.com/shorts/dQw4w9WgXcQ")
        with pytest.raises(ValueError):
            perf.add_snapshot(db, pub, source="manual", views=-1)


def _youtube_api(calls: list):
    def handler(req: httpx.Request) -> httpx.Response:
        calls.append((req.method, req.url.host, req.url.path))
        if req.url.host == "www.googleapis.com":
            assert req.url.params["part"] == "snippet,statistics"
            return httpx.Response(
                200,
                json={
                    "items": [
                        {
                            "id": "dQw4w9WgXcQ",
                            "snippet": {"publishedAt": "2026-09-20T10:00:00Z"},
                            "statistics": {"viewCount": "5400", "likeCount": "300"},
                        }
                    ]
                },
            )
        if req.url.host == "oauth2.googleapis.com":
            assert b"grant_type=refresh_token" in req.content
            return httpx.Response(200, json={"access_token": "yt-token"})
        if req.url.host == "youtubeanalytics.googleapis.com":
            assert req.headers["authorization"] == "Bearer yt-token"
            assert req.url.params["filters"] == "video==dQw4w9WgXcQ"
            return httpx.Response(
                200,
                json={
                    "columnHeaders": [
                        {"name": n}
                        for n in (
                            "video",
                            "views",
                            "likes",
                            "comments",
                            "shares",
                            "averageViewDuration",
                            "averageViewPercentage",
                        )
                    ],
                    "rows": [["dQw4w9WgXcQ", 5000, 290, 12, 40, 21.5, 83.2]],
                },
            )
        return httpx.Response(404)

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_track_merges_public_counts_and_analytics(platform, vsettings):
    s = vsettings.model_copy(
        update={
            "youtube_oauth_client_id": "cid",
            "youtube_oauth_client_secret": SecretStr("sec"),
            "youtube_oauth_refresh_token": SecretStr("ref"),
        }
    )
    calls = []
    with platform.db.session() as db:
        p = VideoProject(topic="t", status="approved")
        db.add(p)
        db.flush()
        pub = perf.record(db, p, "https://youtu.be/dQw4w9WgXcQ")
        perf.record(db, p, "https://www.tiktok.com/@me/video/1")  # no TikTok credentials
        res = Pipeline(platform, s, _youtube_api(calls)).track(db)
        assert res.updated == 1 and len(res.notes) == 1
        assert res.notes[0].startswith("TikTok: 1 recent video(s); set TIKTOK_CLIENT_KEY")
        snap = db.query(MetricSnapshot).one()
        assert (snap.views, snap.likes, snap.comments, snap.shares) == (5400, 300, 12, 40)
        assert snap.avg_view_pct == 83.2 and snap.avg_view_seconds == 21.5
        assert pub.published_at == datetime(2026, 9, 20, 10, 0)  # from the API
    assert all(m == "GET" or h == "oauth2.googleapis.com" for m, h, _ in calls)  # read-only


def test_track_without_keys_explains(platform, no_keys):
    with platform.db.session() as db:
        p = VideoProject(topic="t", status="approved")
        db.add(p)
        db.flush()
        perf.record(db, p, "https://youtu.be/dQw4w9WgXcQ")
        res = Pipeline(platform, no_keys, _vconf.offline()).track(db)
        assert res.updated == 0 and "YOUTUBE_API_KEY" in res.notes[0]


def test_import_csv_matches_by_url_and_reports_bad_rows(platform):
    with platform.db.session() as db:
        p = VideoProject(topic="t", status="approved")
        db.add(p)
        db.flush()
        pub = perf.record(db, p, "https://www.tiktok.com/@me/video/1")
        text = (
            "﻿Video link,Video views,Likes,Comments,Shares,Average percentage viewed,Date\n"
            'https://tiktok.com/@me/video/1/,"12,400",800,30,55,64%,2026-09-25\n'
            "https://tiktok.com/@me/video/999,10,,,,,\n"
            "https://tiktok.com/@me/video/1,nan,,,,,\n"
        )
        n, errors = perf.import_csv(db, text)
        assert n == 1
        assert "isn't recorded" in errors[0] and "Row 4" in errors[1]
        snap = db.query(MetricSnapshot).filter_by(publication_id=pub.id).one()
        assert (snap.views, snap.shares, snap.avg_view_pct) == (12400, 55, 64.0)
        assert snap.taken_at == datetime(2026, 9, 25)
        assert perf.import_csv(db, "a,b\n1,2\n")[1] == [
            "The CSV needs a url column and a views column."
        ]


def _history(db, rows):
    """rows: (trend source, topic, views at 80 h)."""
    for i, (source, topic, views) in enumerate(rows):
        t = Trend(
            source=source,
            title=topic,
            topic=topic,
            signal={},
            score=50,
            breakdown={"velocity": views / 10000, "freshness": 0.5, "relevance": 0.5},
        )
        db.add(t)
        db.flush()
        p = VideoProject(topic=topic, status="approved", trend_id=t.id, template="bold")
        db.add(p)
        db.flush()
        pub = Publication(
            project_id=p.id,
            platform="youtube",
            url=f"https://www.youtube.com/watch?v=vid{i:08d}",
            format="9:16",
            published_at=utcnow() - timedelta(hours=100),
        )
        db.add(pub)
        db.flush()
        db.add(_snap(pub, 80, views))
    db.flush()


def test_report_learns_sources_and_words(platform, vsettings):
    with platform.db.session() as db:
        _history(
            db,
            [
                ("reddit", "GPU prices leak", 9000),
                ("reddit", "GPU benchmark leak", 8000),
                ("reddit", "GPU cooler test", 7000),
                ("google_trends", "Chip stock drop", 900),
                ("google_trends", "Chip export rules", 1100),
                ("youtube", "Phone review", 2000),
            ],
        )
        r = perf.report(db, vsettings)
        assert r.medians["youtube"] == 4050  # 72 h of an 80 h snapshot, interpolated
        assert len(r.outcomes) == 6 and r.active
        top = r.groups["source"][0]
        assert top.value == "reddit" and top.n == 3 and top.multiplier > 1.3
        assert r.feedback.sources["google_trends"] < 0
        assert r.feedback.words["gpu"] > 0 and "chip" not in r.feedback.words  # niche word
        assert "phone" not in r.feedback.words  # seen once: not used for scoring
        assert r.correlations["velocity"] == 1.0  # breakdown ranks match outcomes exactly
        assert r.groups["format"][0].value == "9:16"
        row = next(x for x in r.rows if x.project.topic == "GPU prices leak")
        assert row.window_views == 8100 and row.lift == pytest.approx(2, rel=0.01)
        off = vsettings.model_copy(update={"learn_min_videos": 7})
        assert not perf.report(db, off).active and perf.feedback(db, off) is None


def test_feedback_nudges_scores_within_bounds():
    now = datetime(2026, 9, 27, tzinfo=UTC)
    sigs = [
        TrendSignal(
            source="reddit", title="New GPU leak", topic="gpu", published_at=now, likes=100
        ),
        TrendSignal(
            source="google_trends", title="New GPU leak", topic="gpu", published_at=now, views=4000
        ),
    ]
    fb = Feedback(sources={"reddit": 5.0, "google_trends": -0.1}, words={"gpu": 0.1})
    plain = {s.source: sc for s, sc, _ in score_all(sigs, "gpu", now=now)}
    nudged = {
        s.source: (sc, parts) for s, sc, parts in score_all(sigs, "gpu", now=now, feedback=fb)
    }
    assert nudged["reddit"][1]["track_record"] == 1.248  # capped at ×1.25
    assert nudged["google_trends"][1]["track_record"] == 1.0
    assert nudged["reddit"][0] == pytest.approx(min(100, plain["reddit"] * 1.248), abs=0.2)
    assert "track_record" not in score_all(sigs, "gpu", now=now)[0][2]


def test_weight_overrides_normalize_and_validate():
    w = weights({"velocity": 0, "relevance": 1, "bogus": 9})
    assert w["velocity"] == 0 and abs(sum(w.values()) - 1) < 1e-9 and "bogus" not in w
    with pytest.raises(ValueError):
        weights({k: 0 for k in ("velocity", "freshness", "relevance", "opportunity")})


def test_sweep_applies_track_record(platform, vsettings, media_files):
    pipe = Pipeline(platform, vsettings, _vconf.fake_apis(media_files, []))
    with platform.db.session() as db:
        _history(
            db,
            [
                ("reddit", "benchmarks leaked", 9000),
                ("reddit", "benchmarks leaked again", 8000),
                ("reddit", "cooler test", 7000),
                ("youtube", "explained today", 900),
                ("youtube", "explained again", 1100),
            ],
        )
        trends = {t.source: t for t in pipe.sweep(db)}
        assert trends["reddit"].breakdown["track_record"] > 1
        assert trends["youtube"].breakdown["track_record"] < 1
        assert trends["tavily"].breakdown["track_record"] == 1.0


def test_admin_publications_and_performance(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from redblue.core.config import Settings
    from redblue.platform.app import create_app
    from redblue.platform.seed import ensure_admin
    from redblue.video import config as vconfig

    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("YOUTUBE_API_KEY", raising=False)
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
        p = VideoProject(topic="Heat pumps", status="approved", renders={"9:16": "x"})
        db.add(p)
        db.flush()
        pid = p.id
    c = TestClient(app)
    c.get("/admin/login")
    tok = c.cookies["rb_csrf"]
    c.post(
        "/admin/login",
        data={"email": "admin@example.com", "password": "correct-horse-battery", "csrf_token": tok},
    )
    url = f"/admin/video/projects/{pid}"
    assert "Record publication" in c.get(url).text
    r = c.post(
        f"{url}/publications",
        data={
            "csrf_token": tok,
            "url": "https://youtu.be/dQw4w9WgXcQ",
            "format": "9:16",
            "published_on": "2026-09-20",
        },
    )
    assert "Recorded on youtube" in r.text
    r = c.post(f"{url}/publications", data={"csrf_token": tok, "url": "javascript:alert(1)"})
    assert "must start with https://" in r.text
    r = c.post(
        f"{url}/publications",
        data={"csrf_token": tok, "url": "https://www.tiktok.com/@me/video/7"},
    )
    assert "import a CSV" in r.text
    with app.state.rb.db.session() as db:
        tiktok = db.query(Publication).filter_by(platform="tiktok").one().id
    r = c.post(
        f"/admin/video/publications/{tiktok}/metrics",
        data={"csrf_token": tok, "views": "1,500", "likes": "90", "avg_view_pct": "71%"},
    )
    assert "Numbers saved" in r.text and "1,500 views" in r.text and "71.0% viewed" in r.text
    r = c.post(
        f"/admin/video/publications/{tiktok}/metrics", data={"csrf_token": tok, "views": "-3"}
    )
    assert "whole numbers" in r.text

    page = c.get("/admin/video/performance")
    assert page.status_code == 200 and "Heat pumps" in page.text and "too early" in page.text
    assert 'href="/admin/video/performance"' in c.get("/admin/video").text
    r = c.post(
        "/admin/video/performance/import",
        data={"csrf_token": tok},
        files={"file": ("s.csv", b"url,views\nhttps://www.tiktok.com/@me/video/7,2500\n")},
    )
    assert "Imported 1 row" in r.text
    r = c.post("/admin/video/performance/track", data={"csrf_token": tok})
    assert "YOUTUBE_API_KEY" in r.text
    # CSRF is enforced on the new forms too.
    assert c.post(f"/admin/video/publications/{tiktok}/delete").status_code == 403
    r = c.post(f"/admin/video/publications/{tiktok}/delete", data={"csrf_token": tok})
    assert "Publication removed" in r.text
    with app.state.rb.db.session() as db:
        assert db.query(Publication).count() == 1
        assert db.query(MetricSnapshot).filter_by(publication_id=tiktok).count() == 0
    vconfig.get_video_settings.cache_clear()
