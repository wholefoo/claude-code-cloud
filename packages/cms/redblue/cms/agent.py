"""Content agent: drafts, edits and refreshes CMS entries. Output is always a draft for
human review, validated against the block schemas before it is saved."""

from __future__ import annotations

from pydantic import BaseModel, Field
from redblue.cms import service
from redblue.cms.blocks import Block
from redblue.cms.collections import get_collection
from redblue.cms.models import Entry
from redblue.cms.seo import SEOFields
from redblue.core.ai import untrusted
from redblue.core.context import Platform
from sqlalchemy.orm import Session

SYSTEM = """You are the RedBlue content agent. You write clear, accurate, people-first web
content in structured blocks. Rules:
- Lead with an `answer` block that answers the main question directly in 1-3 sentences.
- Use headings, short paragraphs, lists, and FAQ/HowTo blocks only where they genuinely fit.
- Never invent facts, statistics, quotes, testimonials, reviews, or citations. If a fact is
  needed but unknown, write a paragraph starting with "TODO(editor):" describing what to add.
- No keyword stuffing, hidden text, or claims you cannot support.
- Every image needs meaningful alt text; prefer no image over a placeholder."""


class ContentDraft(BaseModel):
    title: str = Field(max_length=120)
    summary: str = Field(max_length=300)
    blocks: list[Block]
    seo: SEOFields
    tags: list[str] = Field(default_factory=list, max_length=10)


class ContentAgent:
    def __init__(self, platform: Platform):
        self.platform = platform

    def _generate(self, prompt: str, task: str) -> ContentDraft | None:
        ai = self.platform.ai
        if not ai.available:
            return None
        return ai.structured(
            agent="content",
            model=self.platform.settings.models.content,
            system=SYSTEM,
            prompt=prompt,
            output=ContentDraft,
            task=task,
        )

    def draft(
        self, db: Session, collection: str, brief: str, target_questions: list[str] | None = None
    ) -> Entry:
        coll = get_collection(collection)
        questions = target_questions or []
        prompt = (
            f"Write a {coll.label.rstrip('s').lower()} for the site "
            f"{self.platform.settings.site_name!r}.\n"
            f"Target questions: {questions}\n\n{untrusted('brief', brief)}"
        )
        draft = self._generate(prompt, f"draft:{collection}") or _fallback(brief, questions)
        data = service.EntryInput(
            title=draft.title,
            summary=draft.summary,
            blocks=[b.model_dump(exclude_none=True) for b in draft.blocks],
            seo=draft.seo.model_copy(
                update={"target_questions": questions or draft.seo.target_questions}
            ),
            tags=draft.tags,
            ai_label=True,
        )
        return service.create_entry(db, collection, data, None, agent="content")

    def refresh(self, db: Session, entry: Entry, instructions: str = "") -> Entry:
        """Propose an updated version as a new draft revision (live page is untouched)."""
        current = service.entry_text(entry, live=False)
        prompt = (
            "Refresh this page: fix stale facts (mark unknowns with TODO(editor):), improve "
            "the direct answer, and keep what already works.\n"
            f"Editor instructions: {instructions or 'none'}\n\n"
            f"{untrusted('cms-entry', current)}"
        )
        draft = self._generate(prompt, f"refresh:{entry.id}")
        if draft is None:
            return entry
        data = service.EntryInput(
            title=draft.title,
            slug=entry.slug,
            summary=draft.summary,
            blocks=[b.model_dump(exclude_none=True) for b in draft.blocks],
            data=entry.data,
            seo=draft.seo,
            tags=draft.tags or entry.tags,
            category=entry.category,
            locale=entry.locale,
            ai_label=True,
        )
        return service.update_entry(
            db, entry, data, None, note="Refresh proposed by content agent", agent="content"
        )


def _fallback(brief: str, questions: list[str]) -> ContentDraft:
    """Without an API key: a structured outline for a human to fill in."""
    title = brief.strip().split("\n")[0][:110] or "Untitled draft"
    blocks: list[dict] = []
    for q in questions or [f"What is {title}?"]:
        blocks.append(
            {
                "type": "answer",
                "question": q,
                "answer": "TODO(editor): write a direct 1-3 sentence answer.",
            }
        )
    blocks += [
        {"type": "heading", "level": 2, "text": "Overview"},
        {"type": "paragraph", "text": f"TODO(editor): expand on the brief: {brief[:500]}"},
    ]
    return ContentDraft.model_validate(
        {
            "title": title,
            "summary": "TODO(editor): one-sentence summary.",
            "blocks": blocks,
            "seo": {"target_questions": questions},
            "tags": [],
        }
    )
