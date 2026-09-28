import json
from datetime import timedelta

import httpx
import pytest
from pydantic import SecretStr

from redblue.core.db import utcnow
from redblue.video import clients, stats
from redblue.video import performance as perf
from redblue.video.models import MetricSnapshot, VideoProject
from redblue.video.pipeline import Pipeline

URLS = {
    "tiktok": "https://www.tiktok.com/@me/video/7300000000000000001",
    "instagram": "https://www.instagram.com/reel/ABC123/",
    "facebook": "https://www.facebook.com/reel/555000111",
    "threads": "https://www.threads.net/@me/post/DxYz",
    "pinterest": "https://www.pinterest.com/pin/9876543210/",
    "vimeo": "https://vimeo.com/900001",
    "dailymotion": "https://www.dailymotion.com/video/x9abcde",
    "x": "https://x.com/me/status/1800000000000000001",
}
EXPECTED = {  # (views, likes, comments, shares)
    "tiktok": (1500, 90, 7, 4),
    "instagram": (900, 40, 5, 3),
    "facebook": (1200, 35, None, None),
    "threads": (640, 22, 6, 5),
    "pinterest": (321, None, None, None),
    "vimeo": (77, 3, 1, None),
    "dailymotion": (250, 9, None, None),
    "x": (4000, 60, 8, 12),
}


def _settings(vsettings, tmp_path, **extra):
    token_file = tmp_path / "x-token"
    token_file.write_text("x-refresh")
    return vsettings.model_copy(
        update={
            "tiktok_client_key": "tk",
            "tiktok_client_secret": SecretStr("ts"),
            "tiktok_refresh_token": SecretStr("tr"),
            "instagram_access_token": SecretStr("ig-token"),
            "instagram_user_id": "1789",
            "facebook_page_access_token": SecretStr("fb-token"),
            "facebook_page_id": "4040",
            "threads_access_token": SecretStr("th-token"),
            "threads_user_id": "42",
            "pinterest_app_id": "pa",
            "pinterest_app_secret": SecretStr("ps"),
            "pinterest_refresh_token": SecretStr("pr"),
            "pinterest_board_id": "123456",
            "vimeo_access_token": SecretStr("vm-token"),
            "dailymotion_api_key": "dm-key",
            "dailymotion_api_secret": SecretStr("dm-secret"),
            "dailymotion_channel_id": "x2chan",
            "x_client_id": "xc",
            "x_token_file": token_file,
            "x_stats": True,
            **extra,
        }
    )


def _metric(name, value, total=False):
    return (
        {"name": name, "total_value": {"value": value}}
        if total
        else {
            "name": name,
            "values": [{"value": value}],
        }
    )


class FakePlatforms:
    """Every platform's read endpoints. Anything that isn't a read is a test failure."""

    def __init__(self):
        self.calls: list[httpx.Request] = []
        self.fail: set[str] = set()

    def handler(self, req: httpx.Request) -> httpx.Response:
        self.calls.append(req)
        host, path = req.url.host, req.url.path
        if path.endswith(
            ("/oauth/token/", "/oauth/token", "/oauth2/token", "/oauth/v1/token", "/access_token")
        ):
            return httpx.Response(200, json={"access_token": f"{host}-at"})
        if host in self.fail:
            return httpx.Response(400, json={"error": {"message": "Unsupported get request"}})
        assert req.method == "GET" or path == "/v2/video/query/", f"not a read: {req.url}"
        if host == "open.tiktokapis.com":
            assert req.url.params["fields"] == "id,view_count,like_count,comment_count,share_count"
            ids = json.loads(req.content)["filters"]["video_ids"]
            assert ids == ["7300000000000000001"]
            video = {
                "id": ids[0],
                "view_count": 1500,
                "like_count": 90,
                "comment_count": 7,
                "share_count": 4,
            }
            return httpx.Response(200, json={"data": {"videos": [video]}, "error": {"code": "ok"}})
        if host == "graph.facebook.com":
            if path.endswith("/1789/media"):
                return httpx.Response(
                    200,
                    json={
                        "data": [
                            {"id": "1790", "permalink": "https://www.instagram.com/reel/ABC123/"},
                            {"id": "1791", "permalink": "https://www.instagram.com/p/OTHER/"},
                        ],
                        "paging": {"cursors": {"after": "c1"}},
                    },
                )
            if path.endswith("/1790/insights"):
                assert req.url.params["metric"] == "views,likes,comments,shares,saved"
                data = [
                    _metric("views", 900),
                    _metric("likes", 40),
                    _metric("comments", 5),
                    _metric("shares", 3),
                    _metric("saved", 11),
                ]
                return httpx.Response(200, json={"data": data})
            if path.endswith("/555000111/video_insights"):
                data = [
                    _metric("blue_reels_play_count", 1200),
                    _metric("post_video_likes_by_reaction_type", {"REACTION_LIKE": 30, "LOVE": 5}),
                ]
                return httpx.Response(200, json={"data": data})
        if host == "graph.threads.net":
            if path == "/v1.0/42/threads":
                link = "https://www.threads.net/@me/post/DxYz"
                return httpx.Response(200, json={"data": [{"id": "4201", "permalink": link}]})
            if path == "/v1.0/4201/insights":
                data = [
                    _metric(n, v, total=True)
                    for n, v in (
                        ("views", 640),
                        ("likes", 22),
                        ("replies", 6),
                        ("reposts", 2),
                        ("quotes", 1),
                        ("shares", 2),
                    )
                ]
                return httpx.Response(200, json={"data": data})
        if host == "api.pinterest.com" and path == "/v5/pins/9876543210/analytics":
            assert "VIDEO_MRC_VIEW" in req.url.params["metric_types"]
            summary = {"VIDEO_MRC_VIEW": 321, "IMPRESSION": 5000, "SAVE": 4}
            return httpx.Response(200, json={"all": {"summary_metrics": summary}})
        if host == "api.vimeo.com" and path == "/videos/900001":
            return httpx.Response(
                200,
                json={
                    "stats": {"plays": 77},
                    "metadata": {"connections": {"likes": {"total": 3}, "comments": {"total": 1}}},
                },
            )
        if host == "partner.api.dailymotion.com" and path == "/rest/video/x9abcde":
            assert req.url.params["fields"] == "views_total,likes_total"
            return httpx.Response(200, json={"views_total": 250, "likes_total": 9})
        if host == "api.x.com" and path == "/2/tweets":
            assert req.url.params["tweet.fields"] == "public_metrics"
            metrics = {
                "impression_count": 4000,
                "like_count": 60,
                "reply_count": 8,
                "retweet_count": 9,
                "quote_count": 3,
            }
            data = [{"id": req.url.params["ids"], "public_metrics": metrics}]
            return httpx.Response(200, json={"data": data})
        if host == "oauth.reddit.com" and path == "/api/info":
            assert req.url.params["id"] == "t3_1abcde"
            post = {"id": "1abcde", "score": 321, "num_comments": 45, "num_crossposts": 2}
            return httpx.Response(200, json={"data": {"children": [{"data": post}]}})
        if host == "public.api.bsky.app":
            assert "authorization" not in req.headers  # public read, no login
            if path.endswith("identity.resolveHandle"):
                assert req.url.params["handle"] == "me.bsky.social"
                return httpx.Response(200, json={"did": "did:plc:" + "a" * 24})
            if path.endswith("feed.getPosts"):
                uri = "at://did:plc:" + "a" * 24 + "/app.bsky.feed.post/3kabcdefghij2"
                assert req.url.params.get_list("uris") == [uri]
                counts = {"likeCount": 18, "replyCount": 4, "repostCount": 3, "quoteCount": 1}
                return httpx.Response(200, json={"posts": [{"uri": uri, **counts}]})
        if host == "api.tumblr.com" and path == "/v2/blog/myblog/posts":
            assert req.url.params["id"] == "765432100"
            return httpx.Response(200, json={"response": {"posts": [{"note_count": 57}]}})
        return httpx.Response(404, json={"error": {"message": f"unexpected {req.url}"}})

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self.handler))


def _record_all(db, urls=URLS):
    p = VideoProject(topic="t", status="approved")
    db.add(p)
    db.flush()
    return {k: perf.record(db, p, u) for k, u in urls.items()}


def test_every_configured_platform_gets_a_snapshot(platform, vsettings, tmp_path):
    s, fake = _settings(vsettings, tmp_path), FakePlatforms()
    with platform.db.session() as db:
        pubs = _record_all(db)
        res = stats.track(db, s, fake.client())
        assert res.notes == [] and res.updated == len(URLS)
        for name, pub in pubs.items():
            snap = db.query(MetricSnapshot).filter_by(publication_id=pub.id).one()
            assert snap.source == f"{name}_api"
            assert (snap.views, snap.likes, snap.comments, snap.shares) == EXPECTED[name], name
        assert pubs["instagram"].external_id == "1790" and pubs["threads"].external_id == "4201"

        fake.calls.clear()  # ids are kept, so the second run doesn't list posts again
        stats.track(db, s, fake.client())
        assert not any(r.url.path.endswith(("/media", "/threads")) for r in fake.calls)
        assert db.query(MetricSnapshot).count() == 2 * len(URLS)


def test_x_is_opt_in_and_missing_credentials_are_explained(platform, vsettings, tmp_path):
    s = _settings(vsettings, tmp_path, x_stats=False, vimeo_access_token=None)
    fake = FakePlatforms()
    with platform.db.session() as db:
        _record_all(db, {k: URLS[k] for k in ("x", "vimeo", "dailymotion")})
        p = VideoProject(topic="u", status="approved")
        db.add(p)
        db.flush()
        perf.record(db, p, "https://www.reddit.com/r/energy/comments/abc/heat/")
        res = stats.track(db, s.model_copy(update={"reddit_client_id": None}), fake.client())
    assert res.updated == 1  # Dailymotion only
    assert any(n.startswith("Vimeo: 1 recent video(s); set VIMEO_ACCESS_TOKEN") for n in res.notes)
    assert any("RB_VIDEO_X_STATS=true" in n for n in res.notes)
    assert any(n.startswith("Reddit: 1 recent video(s); set REDDIT_CLIENT_ID") for n in res.notes)
    assert len(res.notes) == 3 and not any(r.url.host == "api.x.com" for r in fake.calls)


NO_VIEW_URLS = {
    "reddit": "https://www.reddit.com/r/energy/comments/1abcde/heat_pumps/",
    "bluesky": "https://bsky.app/profile/me.bsky.social/post/3kabcdefghij2",
    "tumblr": "https://www.tumblr.com/myblog/765432100/heat-pumps",
}


def test_platforms_without_views_store_engagement_only(platform, vsettings, tmp_path):
    s = _settings(
        vsettings,
        tmp_path,
        tumblr_client_id="tc",
        tumblr_client_secret=SecretStr("ts"),
        tumblr_refresh_token=SecretStr("tr"),
        tumblr_blog="myblog",
    )
    fake = FakePlatforms()
    with platform.db.session() as db:
        pubs = _record_all(db, {**NO_VIEW_URLS, "vimeo": URLS["vimeo"]})
        other = perf.record(
            db,
            db.get(VideoProject, pubs["vimeo"].project_id),
            "https://www.tumblr.com/else/7654321",
        )  # someone else's blog: can't be read with your token
        res = stats.track(db, s, fake.client())
        assert res.updated == 4
        assert res.notes == ["Tumblr: no numbers for 1 video(s) (not found)."]
        expected = {  # (likes, comments, shares); views are never reported
            "reddit": (321, 45, 2),
            "bluesky": (18, 4, 4),
            "tumblr": (57, None, None),
        }
        for name, counts in expected.items():
            snap = db.query(MetricSnapshot).filter_by(publication_id=pubs[name].id).one()
            assert snap.no_views is True and snap.views == 0, name
            assert (snap.likes, snap.comments, snap.shares) == counts, name
        assert db.query(MetricSnapshot).filter_by(publication_id=other.id).count() == 0
        vimeo = db.query(MetricSnapshot).filter_by(publication_id=pubs["vimeo"].id).one()
        assert vimeo.no_views is None and vimeo.views == 77

        # Placeholder zeros never count as views: no 72-hour number, lift or engagement.
        for snap in db.query(MetricSnapshot):
            snap.taken_at = snap.taken_at + timedelta(days=4)
        rows = {r.pub.platform: r for r in perf.report(db, s).rows}
        assert rows["vimeo"].window_views is not None
        for name in expected:
            assert rows[name].window_views is None and rows[name].engagement is None, name


def test_a_failing_platform_or_video_doesnt_stop_the_others(platform, vsettings, tmp_path):
    s, fake = _settings(vsettings, tmp_path), FakePlatforms()
    fake.fail = {"graph.threads.net", "api.vimeo.com"}
    with platform.db.session() as db:
        _record_all(db)
        res = stats.track(db, s, fake.client())
    assert res.updated == len(URLS) - 2
    assert any(n.startswith("Threads stats failed: Threads: Unsupported") for n in res.notes)
    assert any(n.startswith("Vimeo: no numbers for 1 video") for n in res.notes)


def test_old_publications_and_hidden_views_are_skipped(platform, vsettings, tmp_path):
    s, fake = _settings(vsettings, tmp_path), FakePlatforms()
    with platform.db.session() as db:
        pubs = _record_all(db, {"vimeo": URLS["vimeo"], "dailymotion": URLS["dailymotion"]})
        pubs["dailymotion"].published_at = utcnow() - timedelta(days=s.track_days + 1)
        res = stats.track(db, s, fake.client())
        assert res.updated == 1
        assert not any(r.url.host == "partner.api.dailymotion.com" for r in fake.calls)


def test_pipeline_track_includes_platforms_and_stays_read_only(platform, vsettings, tmp_path):
    s, fake = _settings(vsettings, tmp_path), FakePlatforms()
    with platform.db.session() as db:
        _record_all(db, {"vimeo": URLS["vimeo"]})
        res = Pipeline(platform, s, fake.client()).track(db)
        assert res.updated == 1
    reads = [r for r in fake.calls if not r.url.path.endswith(("token", "token/"))]
    assert reads and all(r.method == "GET" for r in reads)


@pytest.mark.parametrize(
    "body, expected",
    [
        ({"data": [{"name": "views", "values": [{"value": 5}]}]}, {"views": 5}),
        ({"data": [{"name": "views", "total_value": {"value": 6}}]}, {"views": 6}),
        ({"data": [{"name": "likes", "values": [{"value": {"a": 2, "b": 3}}]}]}, {"likes": 5}),
        ({"data": [{"name": "views", "values": [{"value": -1}]}, {"x": 1}, "junk"]}, {}),
    ],
)
def test_insight_values(body, expected):
    assert clients.insight_values(body) == expected
