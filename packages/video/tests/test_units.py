from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from redblue.video.clients import Firecrawl, MissingKey, Pexels, Tavily
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
    assert ranked[0] == fast.title and ranked[-1] == slow.title
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
