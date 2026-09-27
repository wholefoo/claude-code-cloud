"""Headless REST API: every collection is readable as typed JSON; writes need an API key or
session with the right role and always go through the same workflow as the admin UI."""


from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from redblue.cms import service
from redblue.cms.collections import COLLECTIONS, get_collection
from redblue.cms.models import Entry
from redblue.core.auth import Role, User, require_role
from redblue.core.context import get_db
from sqlalchemy.orm import Session


class EntryOut(BaseModel):
    id: int
    collection: str
    slug: str
    locale: str
    path: str
    title: str
    summary: str
    blocks: list[dict]
    data: dict
    seo: dict
    tags: list[str]
    category: str | None
    published_at: datetime | None
    updated_at: datetime | None

    @classmethod
    def from_live(cls, e: Entry) -> "EntryOut":
        live = e.live or {}
        return cls(
            id=e.id,
            collection=e.collection,
            slug=e.live_slug or e.slug,
            locale=e.locale,
            path=get_collection(e.collection).path_for(e.live_slug or e.slug, e.locale),
            title=live.get("title", ""),
            summary=live.get("summary", ""),
            blocks=live.get("blocks", []),
            data=live.get("data", {}),
            seo=live.get("seo", {}),
            tags=live.get("tags", []),
            category=live.get("category"),
            published_at=e.published_at,
            updated_at=e.content_updated_at,
        )


class DraftOut(BaseModel):
    id: int
    collection: str
    slug: str
    status: str
    title: str


class TransitionIn(BaseModel):
    action: Literal["submit", "approve", "request_changes", "publish", "unpublish"]
    note: str = ""
    at: datetime | None = None


def cms_api_router() -> APIRouter:
    r = APIRouter(prefix="/api/cms", tags=["cms"])
    DB = Annotated[Session, Depends(get_db)]

    @r.get("/collections")
    def list_collections() -> list[dict]:
        return [
            {
                "name": c.name,
                "label": c.label,
                "schema_type": c.schema_type,
                "url_prefix": c.url_prefix,
            }
            for c in COLLECTIONS.values()
        ]

    @r.get("/search")
    def search(db: DB, q: str = Query(min_length=2, max_length=100)) -> list[EntryOut]:
        return [EntryOut.from_live(e) for e in service.search(db, q)]

    @r.get("/{collection}")
    def list_entries(
        collection: str,
        db: DB,
        locale: str | None = None,
        tag: str | None = None,
        limit: int = Query(20, le=100),
        offset: int = Query(0, ge=0),
    ) -> list[EntryOut]:
        _known(collection)
        return [
            EntryOut.from_live(e)
            for e in service.live_entries(
                db, collection, locale=locale, tag=tag, limit=limit, offset=offset
            )
        ]

    @r.get("/{collection}/{slug}")
    def get_entry(collection: str, slug: str, db: DB, locale: str = "en") -> EntryOut:
        _known(collection)
        e = service.live_entry(db, collection, slug, locale)
        if e is None:
            raise HTTPException(404, "Not found")
        return EntryOut.from_live(e)

    Writer = Annotated[User, Depends(require_role(Role.writer))]

    @r.post("/{collection}", status_code=201)
    def create(collection: str, body: service.EntryInput, db: DB, user: Writer) -> DraftOut:
        _known(collection)
        try:
            e = service.create_entry(db, collection, body, user)
        except (service.WorkflowError, ValueError) as exc:
            raise HTTPException(422, str(exc)) from exc
        return _draft(e)

    @r.put("/entries/{entry_id}")
    def update(entry_id: int, body: service.EntryInput, db: DB, user: Writer) -> DraftOut:
        e = _get(db, entry_id)
        try:
            service.update_entry(db, e, body, user)
        except service.PermissionDenied as exc:
            raise HTTPException(403, str(exc)) from exc
        except (service.WorkflowError, ValueError) as exc:
            raise HTTPException(422, str(exc)) from exc
        return _draft(e)

    @r.post("/entries/{entry_id}/transition")
    def transition(entry_id: int, body: TransitionIn, db: DB, user: Writer) -> DraftOut:
        e = _get(db, entry_id)
        try:
            match body.action:
                case "submit":
                    service.submit_for_review(db, e, user)
                case "approve":
                    service.approve(db, e, user)
                case "request_changes":
                    service.request_changes(db, e, user, body.note)
                case "publish":
                    service.publish(db, e, user, at=body.at)
                case "unpublish":
                    service.unpublish(db, e, user)
        except service.PermissionDenied as exc:
            raise HTTPException(403, str(exc)) from exc
        except service.PublishBlocked as exc:
            raise HTTPException(409, {"problems": exc.problems}) from exc
        except service.WorkflowError as exc:
            raise HTTPException(409, str(exc)) from exc
        return _draft(e)

    return r


def _known(collection: str) -> None:
    if collection not in COLLECTIONS:
        raise HTTPException(404, "Unknown collection")


def _get(db: Session, entry_id: int) -> Entry:
    e = db.get(Entry, entry_id)
    if e is None:
        raise HTTPException(404, "Not found")
    return e


def _draft(e: Entry) -> DraftOut:
    return DraftOut(
        id=e.id, collection=e.collection, slug=e.slug, status=e.status.value, title=e.title
    )
