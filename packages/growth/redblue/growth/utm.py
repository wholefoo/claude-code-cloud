"""UTM link builder and content calendar."""

from __future__ import annotations

import re
from collections import defaultdict
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from sqlalchemy import select
from sqlalchemy.orm import Session

from redblue.cms.models import Entry, Status

_TOKEN = re.compile(r"^[A-Za-z0-9_.\-]{1,100}$")


def build_utm_url(
    url: str,
    *,
    source: str,
    medium: str,
    campaign: str,
    term: str | None = None,
    content: str | None = None,
) -> str:
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") and not url.startswith("/"):
        raise ValueError("UTM links need an http(s) URL or a site path.")
    params = {"utm_source": source, "utm_medium": medium, "utm_campaign": campaign}
    if term:
        params["utm_term"] = term
    if content:
        params["utm_content"] = content
    for k, v in params.items():
        if not _TOKEN.match(v):
            raise ValueError(f"{k} may only contain letters, numbers, '.', '-' and '_'.")
    query = dict(parse_qsl(parts.query))
    query.update(params)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


def content_calendar(db: Session) -> dict[str, list[dict]]:
    """Scheduled and published entries by day, plus undated drafts, for the admin calendar."""
    cal: dict[str, list[dict]] = defaultdict(list)
    for e in db.scalars(
        select(Entry).where(
            Entry.status.in_(
                [
                    Status.scheduled,
                    Status.published,
                    Status.approved,
                    Status.in_review,
                    Status.draft,
                ]
            )
        )
    ):
        when = e.publish_at or e.published_at
        key = when.date().isoformat() if when else "unscheduled"
        cal[key].append(
            {"id": e.id, "title": e.title, "collection": e.collection, "status": e.status.value}
        )
    return dict(sorted(cal.items()))
