from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from redblue.video.clients import (
    Firecrawl,
    GoogleTrends,
    MissingKey,
    Pexels,
    Reddit,
    Tavily,
    _parse_traffic,
)
from redblue.video.pipeline import _topic
from redblue.video.render import build_ass
from redblue.video.schemas import Beat, Brief, Script, Source, TrendSignal
from redblue.video.scoring import score_all
from redblue.video.writing import fallback_brief, fallback_script

NOW = datetime(2026, 9, 27, tzinfo=UTC)


def test_scoring_prefers_fast_fresh_relevant():
    fast = TrendSignal(
        source="youtube",
        title="AI chips benchmark shock",
        topic="x",
        views=500_000,
        published_at=NOW - timedelta(hours=10),
    )
    slow = TrendSignal(
        source="youtube",
        title="AI chips history",
        topic="x",
        views=5_000,
        published_at=NOW - timedelta(days=6),
    )
    off = TrendSignal(
        source="youtube",
        title="Celebrity cooking show",
        topic="x",
        views=900_000,
        published_at=NOW - timedelta(hours=10),
    )
    ranked = [s.title for s, _, _ in score_all([slow, off, fast], "AI chips", now=NOW)]
    # Relevant and fast wins; off-niche ranks last however viral it is.
    assert ranked == [fast.title, slow.title, off.title]
    _, score, parts = score_all([fast], "AI chips", now=NOW)[0]
    assert 0 < score <= 100 and set(parts) == {"velocity", "freshness", "relevance", "opportunity"}


def test_brief_facts_must_cite_existing_sources():
    with pytest.raises(ValidationError):
        Brief(
            topic="t",
            angle="a",
            audience="b",
            hooks=["h"],
            facts=[{"claim": "x", "source": 3}],
            sources=[],
        )
    with pytest.raises(ValidationError):
        Source(title="x", url="javascript:alert(1)")


def test_script_validation_flags_uncited_numbers_and_length():
    brief = Brief(
        topic="t",
        angle="a",
        audience="b",
        hooks=["h"],
        sources=[Source(title="s", url="https://e.com")],
    )
    s = Script(
        title="t",
        hook="h",
        cta="c",
        beats=[
            Beat(narration="It is 2x faster.", visual_query="chip", seconds=3),
            Beat(narration="Source says so.", visual_query="chip", seconds=3, sources=[5]),
        ],
    )
    problems = s.validate_against(brief)
    assert any("without citing" in p for p in problems)
    assert any("missing source" in p for p in problems)
    long = Script(
        title="t", hook="h", cta="c", beats=[Beat(narration="x", visual_query="q", seconds=15)] * 7
    )
    assert any("under" in p for p in long.validate_against(brief))


def test_fallbacks_produce_valid_cited_script():
    sources = [
        Source(
            title="A",
            url="https://a.com",
            content="The new chip is twice as fast as the old one. More text.",
        )
    ]
    brief = fallback_brief("new AI chip", sources)
    assert brief.facts and brief.facts[0].source == 0
    script = fallback_script(brief, 40)
    assert script.validate_against(brief) == []
    assert script.beats[1].sources == [0]


def test_clients_require_keys_and_refuse_video_platforms():
    with pytest.raises(MissingKey):
        Tavily(None)
    with pytest.raises(MissingKey):
        Reddit(None, "ua")
    r = Reddit(("id", "secret"), "ua")
    for sub, sort in (("../admin", "hot"), ("tech", "controversial"), ("a b", "hot")):
        with pytest.raises(ValueError):
            r.listing(sub, sort)
    with pytest.raises(ValueError):
        GoogleTrends().trending("US&x=1")
    fc = Firecrawl("k")
    for url in ("https://www.youtube.com/watch?v=x", "https://www.tiktok.com/@a/video/1"):
        with pytest.raises(ValueError):
            fc.scrape(url)
    px = Pexels("k")
    for url in (
        "https://evil.example/x.mp4",
        "http://videos.pexels.com/x.mp4",
        "https://videos.pexels.com.evil.io/x.mp4",
    ):
        with pytest.raises(ValueError):
            px.download(url, "/dev/null")


def test_ass_escapes_override_tags():
    s = Script(
        title="t",
        hook="h",
        cta="c",
        beats=[
            Beat(
                narration=r"{\pos(0,0)}hi there",
                on_screen_text="{\\b1}x",
                visual_query="q",
                seconds=2,
            ),
            Beat(narration="bye", visual_query="q", seconds=2),
        ],
    )
    ass = build_ass(s, [2, 2], 1080, 1920)
    events = ass.split("[Events]")[1]
    assert "{" not in events and "\\pos" not in events


def test_topic_cleanup():
    assert _topic("New AI chip explained | Tech Channel") == "New AI chip explained"
    assert _topic("#shorts Big news today") == "Big news today"


@pytest.mark.parametrize(
    "text,value",
    [
        ("50,000+", 50_000),
        ("2K+", 2_000),
        ("1M+", 1_000_000),
        ("200+", 200),
        ("", None),
        ("lots", None),
    ],
)
def test_parse_traffic(text, value):
    assert _parse_traffic(text) == value


def test_google_trends_rejects_xml_entity_attacks():
    import httpx
    from defusedxml.common import DefusedXmlException

    bomb = (
        b'<?xml version="1.0"?><!DOCTYPE r [<!ENTITY a "aaaa"><!ENTITY b "&a;&a;&a;">]>'
        b"<rss><channel><item><title>&b;</title></item></channel></rss>"
    )
    http = httpx.Client(
        transport=httpx.MockTransport(lambda req: httpx.Response(200, content=bomb))
    )
    with pytest.raises(DefusedXmlException):
        GoogleTrends(http).trending("US")


def test_reddit_reach_estimate_feeds_velocity():
    from redblue.video.scoring import reach

    sig = TrendSignal(
        source="reddit",
        title="t",
        topic="t",
        likes=100,
        comments=50,
        published_at=NOW - timedelta(hours=2),
    )
    assert reach(sig) == (100 + 2 * 50) * 40
    ranked = score_all([sig], "t", now=NOW)
    assert ranked[0][2]["velocity"] > 0.5
