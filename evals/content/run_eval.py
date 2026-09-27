"""Content-agent eval: drafts a set of briefs and checks structural quality.

Without an API key it evaluates the deterministic fallback (useful as a baseline).

    python evals/content/run_eval.py [--json out.json]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

BRIEFS = [
    (
        "guide",
        "How to choose a password manager for a small team",
        ["How do I choose a password manager?"],
    ),
    ("question", "What is a content security policy?", ["What is a content security policy?"]),
    ("post", "Why server-rendered HTML helps SEO", ["Does server-side rendering help SEO?"]),
]


def evaluate() -> dict:
    from redblue.cms.agent import ContentAgent
    from redblue.cms.service import entry_text
    from redblue.core.app import build_platform
    from redblue.core.config import Settings
    from redblue.growth.quality import quality_problems

    tmp = tempfile.mkdtemp()
    platform = build_platform(
        Settings(
            database_url="sqlite://",
            env="test",
            storage_dir=Path(tmp) / "m",
            ANTHROPIC_API_KEY=os.environ.get("ANTHROPIC_API_KEY"),
        )
    )
    rows = []
    with platform.db.session() as db:
        agent = ContentAgent(platform)
        for collection, brief, questions in BRIEFS:
            e = agent.draft(db, collection, brief, questions)
            text = entry_text(e, live=False)
            first = e.blocks[0]["type"] if e.blocks else None
            rows.append(
                {
                    "brief": brief,
                    "answer_first": first == "answer",
                    "is_draft": e.status.value == "draft",
                    "ai_flagged": e.ai_generated,
                    "words": len(text.split()),
                    "publishable": not quality_problems(db, e),
                }
            )
    n = len(rows)
    return {
        "ai": platform.ai.available,
        "answer_first_rate": sum(r["answer_first"] for r in rows) / n,
        "draft_rate": sum(r["is_draft"] for r in rows) / n,
        "publishable_rate": sum(r["publishable"] for r in rows) / n,
        "cost_usd": sum(c["cost_usd"] for c in platform.ai.cost_summary()),
        "rows": rows,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json")
    args = ap.parse_args()
    res = evaluate()
    print(json.dumps({k: v for k, v in res.items() if k != "rows"}, indent=2))
    if args.json:
        Path(args.json).write_text(json.dumps(res, indent=2))
    # Agents must only ever produce drafts.
    return 0 if res["draft_rate"] == 1.0 else 1


if __name__ == "__main__":
    sys.exit(main())
