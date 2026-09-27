"""The per-application service container, stored on ``app.state.rb``."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from fastapi import Request
from redblue.core.config import Settings
from redblue.core.db import Database
from sqlalchemy.orm import Session

if TYPE_CHECKING:
    from redblue.core.ai import AIClient
    from redblue.core.email import EmailSender
    from redblue.core.jobs import JobQueue
    from redblue.core.storage import Storage


@dataclass
class Platform:
    settings: Settings
    db: Database
    storage: Storage
    email: EmailSender
    ai: AIClient
    jobs: JobQueue
    extras: dict[str, Any] = field(default_factory=dict)


def get_platform(request: Request) -> Platform:
    return request.app.state.rb


def get_db(request: Request) -> Iterator[Session]:
    with request.app.state.rb.db.session() as s:
        yield s
