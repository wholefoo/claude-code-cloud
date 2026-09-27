"""CMS operations: create/edit drafts, the writer → editor → publisher workflow, revisions,
rollback, scheduled publishing, and automatic 301s when a published slug changes."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field
from redblue.cms.blocks import blocks_text, validate_blocks
from redblue.cms.collections import get_collection, validate_data
from redblue.cms.models import Entry, Redirect, Revision, Status
from redblue.cms.seo import SEOFields
from redblue.core.auth import Role, User
from redblue.core.db import utcnow
from sqlalchemy import Text, cast, func, or_, select
from sqlalchemy.orm import Session


class WorkflowError(Exception):
    pass


class PermissionDenied(WorkflowError):
    pass


class PublishBlocked(WorkflowError):
    def __init__(self, problems: list[str]):
        super().__init__("; ".join(problems))
        self.problems = problems


PublishCheck = Callable[[Session, Entry], list[str]]
_publish_checks: list[PublishCheck] = []


def register_publish_check(check: PublishCheck) -> PublishCheck:
    """Checks return blocking problems. The growth engine registers its quality threshold."""
    if check not in _publish_checks:
        _publish_checks.append(check)
    return check


def slugify(text: str) -> str:
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:200] or "untitled"


class EntryInput(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    slug: str | None = Field(default=None, pattern=r"^[a-z0-9][a-z0-9-]{0,199}$")
    summary: str = ""
    blocks: list[dict] = Field(default_factory=list)
    data: dict = Field(default_factory=dict)
    seo: SEOFields = Field(default_factory=SEOFields)
    tags: list[str] = Field(default_factory=list)
    category: str | None = None
    locale: str = Field(default="en", pattern=r"^[a-z]{2}(-[A-Z]{2})?$")
    translation_of: int | None = None
    ai_label: bool = False


# ---------------------------------------------------------------- permissions


def _can_edit(user: User, entry: Entry) -> bool:
    if user.has_role(Role.editor):
        return True
    return (
        user.has_role(Role.writer)
        and entry.author_id == user.id
        and entry.status in (Status.draft,)
    )


def _require(user: User | None, role: Role) -> None:
    if user is None or not user.has_role(role):
        raise PermissionDenied(f"Requires role {role.name}.")


# ---------------------------------------------------------------- revisions


def _revise(db: Session, entry: Entry, user: User | None, note: str, agent: str | None) -> None:
    n = (
        db.scalar(
            select(func.coalesce(func.max(Revision.number), 0)).where(Revision.entry_id == entry.id)
        )
        or 0
    )
    db.add(
        Revision(
            entry_id=entry.id,
            number=n + 1,
            snapshot=entry.snapshot(),
            note=note[:300],
            author_id=user.id if user else None,
            by_agent=agent,
        )
    )


def revisions(db: Session, entry_id: int) -> list[Revision]:
    return list(
        db.scalars(
            select(Revision).where(Revision.entry_id == entry_id).order_by(Revision.number.desc())
        )
    )


# ---------------------------------------------------------------- create / update


def create_entry(
    db: Session, collection: str, data: EntryInput, user: User | None, *, agent: str | None = None
) -> Entry:
    """Create a draft. ``agent`` marks agent-written content (always a draft, flagged)."""
    get_collection(collection)
    if agent is None:
        _require(user, Role.writer)
    slug = data.slug or slugify(data.title)
    base, i = slug, 2
    while db.scalar(
        select(Entry.id).where(
            Entry.collection == collection, Entry.slug == slug, Entry.locale == data.locale
        )
    ):
        slug, i = f"{base}-{i}", i + 1
    entry = Entry(
        collection=collection,
        slug=slug,
        locale=data.locale,
        translation_of=data.translation_of,
        title=data.title,
        summary=data.summary,
        blocks=validate_blocks(data.blocks),
        data=validate_data(collection, data.data),
        seo=data.seo.model_dump(exclude_none=True),
        tags=data.tags,
        category=data.category,
        status=Status.draft,
        author_id=user.id if user else None,
        ai_generated=agent is not None,
        ai_label=data.ai_label,
    )
    db.add(entry)
    db.flush()
    _revise(db, entry, user, "Created" + (f" by {agent} agent" if agent else ""), agent)
    return entry


def update_entry(
    db: Session,
    entry: Entry,
    data: EntryInput,
    user: User | None,
    *,
    note: str = "",
    agent: str | None = None,
) -> Entry:
    if agent is None and (user is None or not _can_edit(user, entry)):
        raise PermissionDenied("You cannot edit this entry in its current state.")
    new_slug = data.slug or entry.slug
    if new_slug != entry.slug and db.scalar(
        select(Entry.id).where(
            Entry.collection == entry.collection,
            Entry.slug == new_slug,
            Entry.locale == entry.locale,
            Entry.id != entry.id,
        )
    ):
        raise WorkflowError(f"Slug {new_slug!r} is already used in this collection.")
    entry.title, entry.slug, entry.summary = data.title, new_slug, data.summary
    entry.blocks = validate_blocks(data.blocks)
    entry.data = validate_data(entry.collection, data.data)
    entry.seo = data.seo.model_dump(exclude_none=True)
    entry.tags, entry.category, entry.ai_label = data.tags, data.category, data.ai_label
    if agent:
        entry.ai_generated = True
    # Any edit sends the working copy back through review. The live page is unaffected.
    entry.status = Status.draft
    entry.review_note = ""
    _revise(db, entry, user, note or "Edited", agent)
    return entry


# ---------------------------------------------------------------- workflow


def submit_for_review(db: Session, entry: Entry, user: User) -> Entry:
    if not _can_edit(user, entry) or entry.status != Status.draft:
        raise PermissionDenied("Only drafts you can edit can be submitted for review.")
    entry.status = Status.in_review
    return entry


def request_changes(db: Session, entry: Entry, user: User, note: str) -> Entry:
    _require(user, Role.editor)
    if entry.status not in (Status.in_review, Status.approved):
        raise WorkflowError("Entry is not awaiting review.")
    entry.status, entry.review_note = Status.draft, note
    return entry


def approve(db: Session, entry: Entry, user: User) -> Entry:
    _require(user, Role.editor)
    if entry.status != Status.in_review:
        raise WorkflowError("Only entries in review can be approved.")
    entry.status = Status.approved
    return entry


def publish_problems(db: Session, entry: Entry) -> list[str]:
    problems: list[str] = []
    for check in _publish_checks:
        problems.extend(check(db, entry))
    return problems


def publish(db: Session, entry: Entry, user: User, *, at: datetime | None = None) -> Entry:
    """Human-confirmed publishing. Agents cannot call this: it requires a publisher user."""
    _require(user, Role.publisher)
    if entry.status not in (Status.approved, Status.scheduled):
        raise WorkflowError("Entries must be reviewed and approved before publishing.")
    problems = publish_problems(db, entry)
    if problems:
        raise PublishBlocked(problems)
    if at and at > utcnow():
        entry.status, entry.publish_at = Status.scheduled, at
        return entry
    _go_live(db, entry)
    return entry


def _go_live(db: Session, entry: Entry) -> None:
    coll = get_collection(entry.collection)
    if entry.live_slug and entry.live_slug != entry.slug:
        old = coll.path_for(entry.live_slug, entry.locale)
        new = coll.path_for(entry.slug, entry.locale)
        add_redirect(db, old, new, automatic=True)
    entry.live = entry.snapshot()
    entry.live_slug = entry.slug
    entry.status = Status.published
    entry.publish_at = None
    now = utcnow()
    entry.published_at = entry.published_at or now
    entry.content_updated_at = now


def publish_due(db: Session) -> int:
    """Publish scheduled entries whose time has come (run by the job queue)."""
    due = list(
        db.scalars(
            select(Entry).where(Entry.status == Status.scheduled, Entry.publish_at <= utcnow())
        )
    )
    for entry in due:
        if publish_problems(db, entry):
            entry.status = Status.approved  # hold it for a human to look at
            continue
        _go_live(db, entry)
    return len(due)


def unpublish(db: Session, entry: Entry, user: User) -> Entry:
    _require(user, Role.publisher)
    entry.live, entry.live_slug = None, None
    entry.status = Status.archived
    return entry


def rollback(db: Session, entry: Entry, revision_number: int, user: User) -> Entry:
    _require(user, Role.editor)
    rev = db.scalar(
        select(Revision).where(Revision.entry_id == entry.id, Revision.number == revision_number)
    )
    if rev is None:
        raise WorkflowError("No such revision.")
    snap = rev.snapshot
    data = EntryInput(
        title=snap["title"],
        slug=snap["slug"],
        summary=snap["summary"],
        blocks=snap["blocks"],
        data=snap["data"],
        seo=SEOFields(**snap["seo"]),
        tags=snap["tags"],
        category=snap["category"],
        locale=entry.locale,
        ai_label=snap.get("ai_label", False),
    )
    return update_entry(db, entry, data, user, note=f"Rolled back to revision {revision_number}")


# ---------------------------------------------------------------- redirects


def add_redirect(
    db: Session, from_path: str, to_path: str, *, status_code: int = 301, automatic: bool = False
) -> Redirect:
    if not from_path.startswith("/") or not (
        to_path.startswith("/") or to_path.startswith("https://")
    ):
        raise WorkflowError("Redirects must be from a path to a path or https URL.")
    # Avoid chains and loops: point older redirects at the new target.
    for r in db.scalars(select(Redirect).where(Redirect.to_path == from_path)):
        r.to_path = to_path
    db.execute(Redirect.__table__.delete().where(Redirect.from_path == to_path))
    existing = db.scalar(select(Redirect).where(Redirect.from_path == from_path))
    if existing:
        existing.to_path, existing.status_code = to_path, status_code
        return existing
    r = Redirect(from_path=from_path, to_path=to_path, status_code=status_code, automatic=automatic)
    db.add(r)
    db.flush()
    return r


def resolve_redirect(db: Session, path: str) -> Redirect | None:
    r = db.scalar(select(Redirect).where(Redirect.from_path == path))
    if r:
        r.hits += 1
    return r


# ---------------------------------------------------------------- reading


def live_entry(db: Session, collection: str, slug: str, locale: str = "en") -> Entry | None:
    return db.scalar(
        select(Entry).where(
            Entry.collection == collection,
            Entry.live_slug == slug,
            Entry.locale == locale,
            Entry.live.is_not(None),
        )
    )


def live_entries(
    db: Session,
    collection: str | None = None,
    *,
    locale: str | None = None,
    tag: str | None = None,
    category: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[Entry]:
    q = select(Entry).where(Entry.live.is_not(None))
    if collection:
        q = q.where(Entry.collection == collection)
    if locale:
        q = q.where(Entry.locale == locale)
    if category:
        q = q.where(Entry.category == category)
    q = q.order_by(Entry.sort_order, Entry.published_at.desc()).limit(limit).offset(offset)
    items = list(db.scalars(q))
    if tag:
        items = [e for e in items if tag in (e.live or {}).get("tags", [])]
    return items


def search(db: Session, query: str, limit: int = 20) -> list[Entry]:
    terms = [t for t in re.findall(r"\w+", query.lower()) if len(t) > 1][:8]
    if not terms:
        return []
    q = select(Entry).where(Entry.live.is_not(None))
    for t in terms:
        like = f"%{t}%"
        q = q.where(
            or_(func.lower(Entry.title).like(like), func.lower(cast(Entry.live, Text)).like(like))
        )
    results = list(db.scalars(q.limit(200)))

    def score(e: Entry) -> int:
        live = e.live or {}
        hay_title = live.get("title", "").lower()
        body = blocks_text(live.get("blocks", [])).lower()
        return sum(5 * hay_title.count(t) + body.count(t) for t in terms)

    return sorted(results, key=score, reverse=True)[:limit]


def entry_text(entry: Entry, live: bool = True) -> str:
    src: dict[str, Any] = entry.live if live and entry.live else entry.snapshot()
    return "\n".join(
        [src.get("title", ""), src.get("summary", ""), blocks_text(src.get("blocks", []))]
    )


def word_count(text: str) -> int:
    return len(re.findall(r"\w+", text))
