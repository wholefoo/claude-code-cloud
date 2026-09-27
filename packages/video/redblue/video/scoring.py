"""Trend scoring: velocity, freshness, niche relevance and how crowded the topic is.

Scores are 0–100 with a breakdown so humans can see *why* a trend ranks where it does."""

from __future__ import annotations

import math
import re
from collections import Counter
from datetime import UTC, datetime

from redblue.video.schemas import TrendSignal

WEIGHTS = {"velocity": 0.35, "freshness": 0.2, "relevance": 0.3, "opportunity": 0.15}
STOP = set(
    "the a an and or of to in on for with is are was how why what new this that "
    "your you at by from as be it its vs".split()
)


def tokens(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]{3,}", text.lower()) if w not in STOP}


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def velocity(sig: TrendSignal, now: datetime) -> float:
    """Views per hour on a log scale: 100/h ≈ 0.4, 10k/h ≈ 0.8, 100k/h+ ≈ 1."""
    if not sig.views or not sig.published_at:
        return 0.5 if sig.source != "youtube" else 0.0  # news items: neutral
    hours = max(1.0, (now - _aware(sig.published_at)).total_seconds() / 3600)
    return max(0.0, min(1.0, math.log10(sig.views / hours + 1) / 5))


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


def score_all(
    signals: list[TrendSignal],
    niche: str,
    keywords: list[str] | None = None,
    now: datetime | None = None,
) -> list[tuple[TrendSignal, float, dict]]:
    now = now or datetime.now(UTC)
    keywords = keywords or []
    crowd = Counter(t for s in signals for t in tokens(s.title))
    out = []
    for s in signals:
        parts = {
            "velocity": velocity(s, now),
            "freshness": freshness(s, now),
            "relevance": relevance(s, niche, keywords),
            "opportunity": opportunity(s, crowd),
        }
        total = round(100 * sum(WEIGHTS[k] * v for k, v in parts.items()), 1)
        out.append((s, total, {k: round(v, 3) for k, v in parts.items()}))
    return sorted(out, key=lambda x: -x[1])
