"""White-hat publish guardrails, registered as CMS publish checks.

- Programmatic pages (locations, directories, service areas) need enough unique, useful text
  and must not be near-duplicates of their siblings (no doorway pages).
- Nothing with unresolved TODO(editor) markers ships.
- Comparison pages must carry an honesty disclosure.
"""

from __future__ import annotations

import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from redblue.cms.collections import get_collection
from redblue.cms.models import Entry
from redblue.cms.service import entry_text, word_count


def shingles(text: str, k: int = 5) -> set[int]:
    words = re.findall(r"\w+", text.lower())
    return {hash(" ".join(words[i : i + k])) for i in range(max(0, len(words) - k + 1))}


def jaccard(a: set, b: set) -> float:
    return len(a & b) / len(a | b) if a and b else 0.0


def quality_problems(
    db: Session, entry: Entry, *, min_words: int = 150, max_similarity: float = 0.8
) -> list[str]:
    problems: list[str] = []
    text = entry_text(entry, live=False)
    if "TODO(editor)" in text:
        problems.append("Resolve all TODO(editor) notes before publishing.")
    coll = get_collection(entry.collection)
    if coll.name in ("comparison", "alternative") and not (entry.data or {}).get("disclosure"):
        problems.append(
            "Comparison pages need a disclosure of your relationship to the products compared."
        )
    if coll.programmatic:
        words = word_count(text)
        if words < min_words:
            problems.append(
                f"Thin content: {words} words; programmatic pages need at least "
                f"{min_words} words of unique, useful content."
            )
        mine = shingles(text)
        siblings = db.scalars(
            select(Entry).where(
                Entry.collection == entry.collection, Entry.id != entry.id, Entry.live.is_not(None)
            )
        )
        for other in siblings:
            sim = jaccard(mine, shingles(entry_text(other)))
            if sim >= max_similarity:
                problems.append(
                    f"Near-duplicate of “{other.live['title']}” "
                    f"({sim:.0%} similar). Add information specific to this page."
                )
                break
    return problems
