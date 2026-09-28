"""Database setup: SQLAlchemy 2 with typed models. Postgres in prod, SQLite in dev.

Only parameterized queries are used across the platform; there is no raw-SQL helper.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import DateTime, Engine, create_engine, event, text
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker
from sqlalchemy.pool import StaticPool

MIGRATIONS = Path(__file__).parent / "migrations"
_LOCK_KEY = 72_390_411  # arbitrary, constant: serializes concurrent upgrades on Postgres


def utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


class Base(DeclarativeBase):
    pass


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class Database:
    def __init__(self, url: str):
        kwargs: dict = {}
        if url.startswith("sqlite"):
            kwargs["connect_args"] = {"check_same_thread": False}
            if url in ("sqlite://", "sqlite:///:memory:"):
                kwargs["poolclass"] = StaticPool
        self.engine: Engine = create_engine(url, **kwargs)
        if url.startswith("sqlite"):
            event.listen(self.engine, "connect", _sqlite_pragmas)
        self.sessionmaker = sessionmaker(self.engine, expire_on_commit=False)

    def create_all(self) -> None:
        """Bring the schema up to date: runs every pending migration (see ``upgrade``)."""
        self.upgrade()

    # ------------------------------------------------------------ migrations

    def _alembic(self, connection):
        from alembic.config import Config

        cfg = Config()
        cfg.set_main_option("script_location", str(MIGRATIONS))
        cfg.set_main_option("file_template", "%%(rev)s")  # ids already carry the slug
        cfg.attributes["connection"] = connection  # env.py uses it; the URL isn't passed
        return cfg

    def upgrade(self, revision: str = "head") -> None:
        """Apply pending migrations. Works on empty databases and on ones created before
        migrations existed (the baseline revision brings those up to date first)."""
        from alembic import command

        _import_models()
        with self.engine.begin() as conn:
            if conn.dialect.name == "postgresql":  # several workers may start at once
                conn.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": _LOCK_KEY})
            command.upgrade(self._alembic(conn), revision)

    def current_revision(self) -> str | None:
        from alembic.migration import MigrationContext

        with self.engine.connect() as conn:
            return MigrationContext.configure(conn).get_current_revision()

    def pending_changes(self) -> list:
        """Differences between the models and the upgraded database: empty unless a model
        changed without a migration (``redblue db revision`` writes one)."""
        from alembic.autogenerate import compare_metadata
        from alembic.migration import MigrationContext

        _import_models()
        with self.engine.connect() as conn:
            ctx = MigrationContext.configure(conn, opts={"compare_type": True})
            return compare_metadata(ctx, Base.metadata)

    def revision(self, message: str) -> Path:
        """Write a new migration from the difference between the models and this
        (upgraded) database. Review it before committing: autogenerate can't see renames."""
        from alembic import command
        from alembic.script import ScriptDirectory

        _import_models()
        with self.engine.begin() as conn:
            cfg = self._alembic(conn)
            heads = ScriptDirectory.from_config(cfg).get_heads()
            number = max((int(h.split("_", 1)[0]) for h in heads if h[:4].isdigit()), default=0)
            slug = re.sub(r"[^a-z0-9]+", "_", message.lower()).strip("_")[:40] or "change"
            script = command.revision(
                cfg, message=message, autogenerate=True, rev_id=f"{number + 1:04d}_{slug}"
            )
        return Path(script.path)

    @contextmanager
    def session(self) -> Iterator[Session]:
        s = self.sessionmaker()
        try:
            yield s
            s.commit()
        except Exception:
            s.rollback()
            raise
        finally:
            s.close()


def _sqlite_pragmas(dbapi_conn, _record) -> None:
    cur = dbapi_conn.cursor()
    cur.execute("PRAGMA foreign_keys=ON")
    cur.close()


def _import_models() -> None:
    import importlib

    for mod in (
        "redblue.core.auth",
        "redblue.core.jobs",
        "redblue.core.ai",
        "redblue.core.storage",
        "redblue.cms.models",
        "redblue.growth.models",
        "redblue.observe.models",
        "redblue.video.models",
    ):
        try:
            importlib.import_module(mod)
        except ModuleNotFoundError:
            continue
