"""Alembic environment for RedBlue.

Always run through ``redblue.core.db.Database.upgrade()`` (or ``redblue db ...``), which
passes an open connection in ``config.attributes["connection"]``: the database URL (and any
password in it) never goes through Alembic's config or logs."""

from __future__ import annotations

from alembic import context

from redblue.core.db import Base, _import_models

_import_models()
target_metadata = Base.metadata
connection = context.config.attributes.get("connection")
if connection is None:
    raise RuntimeError("Run migrations with `redblue db upgrade`, not the alembic command.")

context.configure(
    connection=connection,
    target_metadata=target_metadata,
    render_as_batch=True,  # SQLite can't ALTER most things in place; batch mode copies
    compare_type=True,
)
with context.begin_transaction():
    context.run_migrations()
