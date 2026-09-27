"""Internal linking suggestions from content similarity (TF-IDF cosine, no dependencies)."""

from __future__ import annotations

import math
import re
from collections import Counter

from sqlalchemy.orm import Session

from redblue.cms.collections import get_collection
from redblue.cms.models import Entry
from redblue.cms.service import entry_text, live_entries

STOP = set(
    """a an and are as at be but by for from has have how i in is it its of on or
that the this to was were what when where which who why will with you your we our can do
does not""".split()
)


def _tokens(text: str) -> list[str]:
    return [w for w in re.findall(r"[a-z0-9]{3,}", text.lower()) if w not in STOP]


def _vectors(docs: dict[int, str]) -> dict[int, dict[str, float]]:
    tfs = {i: Counter(_tokens(t)) for i, t in docs.items()}
    df = Counter(w for tf in tfs.values() for w in tf)
    n = len(docs)
    vecs = {}
    for i, tf in tfs.items():
        v = {
            w: (c / max(1, sum(tf.values()))) * math.log((1 + n) / (1 + df[w]) + 1)
            for w, c in tf.items()
        }
        norm = math.sqrt(sum(x * x for x in v.values())) or 1.0
        vecs[i] = {w: x / norm for w, x in v.items()}
    return vecs


def _cos(a: dict[str, float], b: dict[str, float]) -> float:
    if len(a) > len(b):
        a, b = b, a
    return sum(x * b.get(w, 0.0) for w, x in a.items())


def related(
    db: Session,
    entry: Entry,
    k: int = 3,
    pool: list[Entry] | None = None,
    default_locale: str = "en",
) -> list[dict]:
    pool = pool if pool is not None else live_entries(db, None, locale=entry.locale, limit=500)
    docs = {e.id: entry_text(e) for e in pool}
    docs.setdefault(entry.id, entry_text(entry))
    vecs = _vectors(docs)
    scored = sorted(
        ((_cos(vecs[entry.id], vecs[e.id]), e) for e in pool if e.id != entry.id),
        key=lambda x: -x[0],
    )
    return [
        {
            "title": e.live["title"],
            "score": round(s, 3),
            "url": get_collection(e.collection).path_for(e.live_slug, e.locale, default_locale),
        }
        for s, e in scored[:k]
        if s > 0.05
    ]


def link_suggestions(db: Session, default_locale: str = "en", limit: int = 50) -> list[dict]:
    """Pages that mention another page's title in their text but don't link to it."""
    pool = live_entries(db, None, limit=1000)
    out = []
    for target in pool:
        title = target.live["title"].strip()
        if len(title) < 5:
            continue
        url = get_collection(target.collection).path_for(
            target.live_slug, target.locale, default_locale
        )
        pattern = re.compile(rf"\b{re.escape(title)}\b", re.I)
        for src in pool:
            if src.id == target.id:
                continue
            raw = str(src.live.get("blocks", []))
            if pattern.search(entry_text(src)) and f"]({url})" not in raw and url not in raw:
                out.append(
                    {"from": src.live["title"], "from_id": src.id, "to": title, "to_url": url}
                )
                if len(out) >= limit:
                    return out
    return out
