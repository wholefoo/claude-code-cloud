"""Question coverage audit: which user questions the site answers well, poorly, or not at
all. Questions come from entries' target questions and Search Console queries."""

from __future__ import annotations

import re

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from redblue.cms.service import live_entries
from redblue.growth.linking import _tokens
from redblue.growth.models import SearchConsoleRow

QUESTION_START = re.compile(
    r"^(who|what|when|where|why|how|which|can|does|do|is|are|should"
    r"|will)\b",
    re.I,
)


def _overlap(q: set[str], text: set[str]) -> float:
    return len(q & text) / len(q) if q else 0.0


def collect_questions(db: Session, limit: int = 300) -> list[str]:
    qs: list[str] = []
    for e in live_entries(db, None, limit=2000):
        qs += (e.live or {}).get("seo", {}).get("target_questions", [])
    for (query,) in db.execute(
        select(SearchConsoleRow.query)
        .group_by(SearchConsoleRow.query)
        .order_by(func.sum(SearchConsoleRow.impressions).desc())
        .limit(limit)
    ):
        if QUESTION_START.match(query):
            qs.append(query)
    return list(dict.fromkeys(q.strip() for q in qs if q.strip()))


def coverage(db: Session, questions: list[str] | None = None) -> dict:
    questions = questions if questions is not None else collect_questions(db)
    answers: list[tuple[set[str], str]] = []
    bodies: list[tuple[set[str], str]] = []
    for e in live_entries(db, None, limit=2000):
        blocks = (e.live or {}).get("blocks", [])
        for b in blocks:
            if b.get("type") == "answer":
                answers.append((set(_tokens(b["question"] + " " + b["answer"])), e.live["title"]))
            elif b.get("type") == "faq":
                for it in b["items"]:
                    answers.append(
                        (set(_tokens(it["question"] + " " + it["answer"])), e.live["title"])
                    )
        from redblue.cms.service import entry_text

        bodies.append((set(_tokens(entry_text(e))), e.live["title"]))
    rows = []
    for q in questions:
        qt = set(_tokens(q))
        best_answer = max(((_overlap(qt, a), t) for a, t in answers), default=(0.0, ""))
        best_body = max(((_overlap(qt, b), t) for b, t in bodies), default=(0.0, ""))
        if best_answer[0] >= 0.6:
            status, page = "well", best_answer[1]
        elif best_body[0] >= 0.5:
            status, page = "poorly", best_body[1]
        else:
            status, page = "missing", ""
        rows.append({"question": q, "status": status, "page": page})
    total = len(rows) or 1
    score = round(
        sum({"well": 1.0, "poorly": 0.5, "missing": 0.0}[r["status"]] for r in rows) / total, 3
    )
    return {"score": score, "questions": rows}


def import_search_console_csv(db: Session, csv_text: str) -> int:
    """Import a Search Console performance export (columns: date, query, page, clicks,
    impressions, position). Returns rows imported."""
    import csv
    import io
    from datetime import date

    n = 0
    for row in csv.DictReader(io.StringIO(csv_text)):
        low = {k.strip().lower(): (v or "").strip() for k, v in row.items() if k}
        try:
            db.add(
                SearchConsoleRow(
                    day=date.fromisoformat(low.get("date", "")[:10]),
                    query=low.get("query", low.get("top queries", ""))[:500],
                    page=low.get("page", low.get("top pages", ""))[:500],
                    clicks=int(float(low.get("clicks", 0) or 0)),
                    impressions=int(float(low.get("impressions", 0) or 0)),
                    position=float(low.get("position", 0) or 0),
                )
            )
            n += 1
        except ValueError:
            continue
    return n
