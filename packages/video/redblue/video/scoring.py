"""Trend scoring: velocity, freshness, niche relevance and how crowded the topic is.

Scores are 0–100 with a breakdown so humans can see *why* a trend ranks where it does.
Reach is views (YouTube), searches (Google Trends) or an estimate from votes and comments
(Reddit), so different sources rank on one scale. Relevance also gates the total, keeping
viral but off-niche items below on-niche ones.

With enough published videos, :class:`Feedback` (built from your own results in
``performance.py``) nudges scores by ×0.8–1.25 for sources and topic words that have done
better or worse than your median. The nudge shows in the breakdown as ``track_record``."""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime

from redblue.video.schemas import TrendSignal

WEIGHTS = {"velocity": 0.35, "freshness": 0.2, "relevance": 0.3, "opportunity": 0.15}
STOP = set(
    "the a an and or of to in on for with is are was how why what new this that "
    "your you at by from as be it its vs".split()
)


def _stem(word: str) -> str:
    """Fold simple plurals so "chip" matches "chips" (not "ss" words like "glass")."""
    if len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    return word


def tokens(text: str) -> set[str]:
    # Two-letter tokens are kept: "ai", "ev", "5g" are often the whole point of a niche.
    return {_stem(w) for w in re.findall(r"[a-z0-9]{2,}", text.lower()) if w not in STOP}


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


# Reddit reports votes and comments, not views. Roughly one visible interaction per ~40
# viewers is a common rule of thumb; comments signal more engagement than a vote.
REDDIT_VIEWS_PER_INTERACTION = 40


def reach(sig: TrendSignal) -> int | None:
    """Views (YouTube), searches (Google Trends) or an equivalent estimate (Reddit)."""
    if sig.views:
        return sig.views
    if sig.source == "reddit" and (sig.likes or sig.comments):
        return ((sig.likes or 0) + 2 * (sig.comments or 0)) * REDDIT_VIEWS_PER_INTERACTION
    return None


def velocity(sig: TrendSignal, now: datetime) -> float:
    """Reach per hour on a log scale: 100/h ≈ 0.4, 10k/h ≈ 0.8, 100k/h+ ≈ 1."""
    views = reach(sig)
    if not views or not sig.published_at:
        return 0.5 if sig.source == "tavily" else 0.0  # news items: neutral
    hours = max(1.0, (now - _aware(sig.published_at)).total_seconds() / 3600)
    return max(0.0, min(1.0, math.log10(views / hours + 1) / 5))


def freshness(sig: TrendSignal, now: datetime, half_life_h: float = 36) -> float:
    if not sig.published_at:
        return 0.5
    hours = max(0.0, (now - _aware(sig.published_at)).total_seconds() / 3600)
    return 0.5 ** (hours / half_life_h)


def relevance(sig: TrendSignal, niche: str, keywords: list[str]) -> float:
    want = tokens(niche + " " + " ".join(keywords))
    have = tokens(f"{sig.title} {sig.snippet} {' '.join(sig.extra.get('tags', []))}")
    if not want:
        return 0.5
    return min(1.0, len(want & have) / max(1, min(len(want), 4)))


def opportunity(sig: TrendSignal, crowd: Counter) -> float:
    """Topics that many signals already cover are more crowded (less room to stand out)."""
    toks = tokens(sig.title)
    if not toks:
        return 0.5
    overlap = sum(crowd[t] for t in toks) / len(toks)
    return 1 / (1 + max(0.0, overlap - 1) / 3)


TRACK_RECORD_LIMIT = 0.32  # log2 bound: multiplier stays within ×0.8 … ×1.25


@dataclass
class Feedback:
    """Learned effects (log2 of performance vs. your median, shrunk toward 0)."""

    sources: dict[str, float] = field(default_factory=dict)
    words: dict[str, float] = field(default_factory=dict)

    def __bool__(self) -> bool:
        return bool(self.sources or self.words)

    def effect(self, sig: TrendSignal) -> float:
        e = self.sources.get(sig.source, 0.0)
        hits = [self.words[t] for t in tokens(sig.title) if t in self.words]
        if hits:
            e += sum(hits) / len(hits)
        return max(-TRACK_RECORD_LIMIT, min(TRACK_RECORD_LIMIT, e))

    def multiplier(self, sig: TrendSignal) -> float:
        return 2 ** self.effect(sig)


def weights(overrides: dict[str, float] | None = None) -> dict[str, float]:
    """Default weights with overrides applied, normalized to sum to 1."""
    w = {**WEIGHTS, **{k: float(v) for k, v in (overrides or {}).items() if k in WEIGHTS}}
    if any(v < 0 for v in w.values()) or sum(w.values()) <= 0:
        raise ValueError("Score weights must be non-negative and not all zero.")
    total = sum(w.values())
    return {k: v / total for k, v in w.items()}


def score_all(
    signals: list[TrendSignal],
    niche: str,
    keywords: list[str] | None = None,
    now: datetime | None = None,
    feedback: Feedback | None = None,
    weight_overrides: dict[str, float] | None = None,
) -> list[tuple[TrendSignal, float, dict]]:
    now = now or datetime.now(UTC)
    keywords = keywords or []
    w = weights(weight_overrides)
    crowd = Counter(t for s in signals for t in tokens(s.title))
    out = []
    for s in signals:
        parts = {
            "velocity": velocity(s, now),
            "freshness": freshness(s, now),
            "relevance": relevance(s, niche, keywords),
            "opportunity": opportunity(s, crowd),
        }
        # Relevance also gates the whole score: an off-niche item, however viral, reaches at
        # most 40% of the score an equally strong on-niche item would get.
        base = sum(w[k] * v for k, v in parts.items())
        total = 100 * base * (0.4 + 0.6 * parts["relevance"])
        if feedback:
            parts["track_record"] = feedback.multiplier(s)
            total = min(100.0, total * parts["track_record"])
        out.append((s, round(total, 1), {k: round(v, 3) for k, v in parts.items()}))
    return sorted(out, key=lambda x: -x[1])
