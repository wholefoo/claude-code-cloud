"""Freshness monitoring: flags pages whose facts or dates have likely gone stale."""

from __future__ import annotations

import re
from datetime import timedelta

from sqlalchemy.orm import Session

from redblue.cms.service import entry_text, live_entries
from redblue.core.db import utcnow

MAX_AGE_DAYS = {
    "post": 365,
    "doc": 180,
    "guide": 270,
    "question": 270,
    "product": 180,
    "comparison": 120,
    "alternative": 120,
    "page": 365,
    "location": 365,
}


def stale_pages(db: Session, now=None) -> list[dict]:
    now = now or utcnow()
    year = now.year
    out = []
    for e in live_entries(db, None, limit=5000):
        reasons = []
        max_age = MAX_AGE_DAYS.get(e.collection)
        updated = e.content_updated_at or e.published_at
        if max_age and updated and now - updated > timedelta(days=max_age):
            reasons.append(f"not updated in {(now - updated).days} days")
        text = entry_text(e)
        old_years = sorted(
            {
                int(y)
                for y in re.findall(r"\b(20\d\d)\b", text)
                if int(y) < year - 1
                and re.search(rf"(in|as of|for|updated|best .* of) {y}", text, re.I)
            }
        )
        if old_years:
            reasons.append(f"mentions dated year(s) {', '.join(map(str, old_years))}")
        if "TODO(editor)" in text:
            reasons.append("contains TODO(editor) notes")
        if reasons:
            out.append(
                {
                    "id": e.id,
                    "title": e.live["title"],
                    "collection": e.collection,
                    "reasons": reasons,
                }
            )
    return out
