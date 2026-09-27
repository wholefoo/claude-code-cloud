"""Brief and script generation. Claude drafts (structured output) when a key is set;
otherwise deterministic fallbacks produce usable, clearly-marked drafts. Web content is
untrusted input and is fenced before it reaches a model."""

from __future__ import annotations

import re

from redblue.core.ai import AIClient, untrusted
from redblue.video.schemas import Beat, Brief, Fact, Script, Source

BRIEF_SYSTEM = """You are the research step of a short-form video pipeline. From the sources,
write a brief for an ORIGINAL explainer video: a fresh angle, the audience, 3 hook options,
and key facts. Every fact must be directly supported by the numbered source it cites; if the
sources disagree or are thin, say so in `gaps` instead of guessing. Never copy another
creator's script or format verbatim."""

SCRIPT_SYSTEM = """You write scripts for original 40-60 second vertical explainer videos.
Structure: a hook in the first 2 seconds, 4-8 beats, a clear call to action. Each beat has
narration (spoken, ~2.5 words per second), short on-screen text, stock-footage search terms,
a duration, and the indices of brief sources it relies on. Any number, statistic or factual
claim must cite a source. No invented facts, no clickbait the video doesn't pay off, no
claims about real people that the sources don't support."""

WORDS_PER_SECOND = 2.5


def make_brief(
    topic: str, sources: list[Source], ai: AIClient | None = None, model: str = "claude-sonnet-5"
) -> Brief:
    if ai is not None and ai.available and sources:
        listing = "\n\n".join(
            untrusted(f"source {i}: {s.title} <{s.url}>", s.content[:6000])
            for i, s in enumerate(sources)
        )
        brief = ai.structured(
            agent="video",
            model=model,
            system=BRIEF_SYSTEM,
            output=Brief,
            task=f"video.brief:{topic[:60]}",
            prompt=f"Topic: {topic}\n\nSources:\n{listing}",
        )
        # Keep our fetched sources (with URLs we control), not the model's copy of them.
        return brief.model_copy(update={"topic": topic, "sources": sources})
    return fallback_brief(topic, sources)


def fallback_brief(topic: str, sources: list[Source]) -> Brief:
    facts = []
    for i, s in enumerate(sources[:6]):
        sentence = _first_sentence(s.content)
        if sentence:
            facts.append(Fact(claim=sentence[:400], source=i))
    return Brief(
        topic=topic,
        angle=f"What {topic} means for you, in under a minute",
        audience="Busy viewers who saw the headline and want the short version",
        hooks=[
            f"Everyone's talking about {topic}. Here's what actually matters.",
            f"{topic} in 45 seconds.",
            f"Three things to know about {topic}.",
        ],
        facts=facts,
        sources=sources,
        gaps="Draft generated without AI: check facts and pick a sharper angle.",
    )


def make_script(
    brief: Brief,
    ai: AIClient | None = None,
    model: str = "claude-sonnet-5",
    target_seconds: int = 50,
) -> Script:
    if ai is not None and ai.available:
        facts = "\n".join(f"- {f.claim} [source {f.source}]" for f in brief.facts)
        prompt = f"Target length: {target_seconds} seconds.\n" + untrusted(
            "brief",
            f"Topic: {brief.topic}\nAngle: {brief.angle}\n"
            f"Audience: {brief.audience}\nHooks: {brief.hooks}\n"
            f"Facts:\n{facts}\nGaps: {brief.gaps}",
        )
        return ai.structured(
            agent="video",
            model=model,
            system=SCRIPT_SYSTEM,
            output=Script,
            task=f"video.script:{brief.topic[:60]}",
            prompt=prompt,
        )
    return fallback_script(brief, target_seconds)


def fallback_script(brief: Brief, target_seconds: int = 50) -> Script:
    beats = [
        Beat(
            narration=brief.hooks[0],
            on_screen_text=_short(brief.topic),
            visual_query=_query(brief.topic),
            seconds=_secs(brief.hooks[0]),
        )
    ]
    for f in brief.facts[:5]:
        beats.append(
            Beat(
                narration=f.claim,
                on_screen_text=_short(f.claim),
                visual_query=_query(f.claim),
                seconds=_secs(f.claim),
                sources=[f.source],
            )
        )
    if len(beats) < 3:
        text = f"Here's the short version of {brief.topic}, with sources in the description."
        beats.append(
            Beat(
                narration=text,
                on_screen_text="The short version",
                visual_query=_query(brief.topic),
                seconds=_secs(text),
            )
        )
    cta = "Follow for more explainers like this."
    beats.append(
        Beat(
            narration=cta,
            on_screen_text="Follow for more",
            visual_query="smartphone scrolling",
            seconds=_secs(cta),
        )
    )
    while sum(b.seconds for b in beats) > target_seconds + 10 and len(beats) > 3:
        beats.pop(-2)
    return Script(
        title=_short(brief.topic, 90), hook=brief.hooks[0][:200], beats=beats, cta=cta, hashtags=[]
    )


def _first_sentence(text: str) -> str:
    text = re.sub(r"\s+", " ", re.sub(r"[#*_`>\[\]]", "", text or "")).strip()
    m = re.match(r"(.{40,300}?[.!?])(\s|$)", text)
    return m.group(1) if m else text[:200]


def _secs(text: str) -> float:
    return round(max(2.0, min(12.0, len(text.split()) / WORDS_PER_SECOND + 0.4)), 1)


def _short(text: str, n: int = 60) -> str:
    text = text.strip()
    return text if len(text) <= n else text[: n - 1].rsplit(" ", 1)[0] + "…"


def _query(text: str) -> str:
    words = [w for w in re.findall(r"[A-Za-z]{4,}", text)][:3]
    return " ".join(words) or "abstract technology"
