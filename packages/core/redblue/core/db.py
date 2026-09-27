"""Database setup: SQLAlchemy 2 with typed models. Postgres in prod, SQLite in dev.

Only parameterized queries are used across the platform; there is no raw-SQL helper.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime

from sqlalchemy import DateTime, Engine, create_engine, event, inspect, text
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker
from sqlalchemy.pool import StaticPool

_IDENT = re.compile(r"[a-z_][a-z0-9_]{0,62}")
_DDL_TYPE = re.compile(r"[A-Z][A-Z0-9_ ]*(\(\d+(,\s*\d+)?\))?")


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
        # Import model modules so they register with Base.metadata.
        _import_models()
        Base.metadata.create_all(self.engine)
        self.add_missing_columns()

    def add_missing_columns(self) -> list[str]:
        """Additive upgrades: add new *nullable* columns to existing tables.

        ``create_all`` never alters existing tables, so a release that adds a column would
        break older databases. This covers the common additive case safely; renames, type
        changes and NOT NULL columns still need a real migration (Alembic, on the roadmap).
        Returns the ``table.column`` names it added.
        """
        insp = inspect(self.engine)
        existing_tables = set(insp.get_table_names())
        quote = self.engine.dialect.identifier_preparer.quote
        added: list[str] = []
        with self.engine.begin() as conn:
            for table in Base.metadata.sorted_tables:
                if table.name not in existing_tables:
                    continue
                present = {c["name"] for c in insp.get_columns(table.name)}
                for col in table.columns:
                    if col.name in present or not col.nullable or col.primary_key:
                        continue
                    # DDL identifiers can't be bound parameters. Names and types come from
                    # our own model metadata (never user input); validate and quote anyway.
                    if not (_IDENT.fullmatch(table.name) and _IDENT.fullmatch(col.name)):
                        raise ValueError(f"Refusing unusual identifier {table.name}.{col.name}")
                    ddl_type = col.type.compile(dialect=self.engine.dialect)
                    if not _DDL_TYPE.fullmatch(ddl_type):
                        raise ValueError(f"Refusing unusual column type {ddl_type!r}")
                    stmt = (
                        f"ALTER TABLE {quote(table.name)} ADD COLUMN {quote(col.name)} {ddl_type}"
                    )
                    conn.execute(text(stmt))
                    added.append(f"{table.name}.{col.name}")
        return added

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
