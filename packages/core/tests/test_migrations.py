import shutil

import pytest
from sqlalchemy import Column, Integer, String, Table, inspect, text

from redblue.core import db as dbmod
from redblue.core.db import Base, Database


def _pre_migration_db(tmp_path) -> Database:
    """A database as older releases made it: tables from the models, no Alembic version."""
    db = Database(f"sqlite:///{tmp_path}/old.db")
    dbmod._import_models()
    Base.metadata.create_all(db.engine)
    return db


def test_fresh_database_matches_the_models(tmp_path):
    """Fails when a model changes without a migration: run `redblue db revision -m ...`."""
    db = Database(f"sqlite:///{tmp_path}/new.db")
    db.create_all()
    assert db.current_revision() == "0001_baseline"
    assert db.pending_changes() == []
    db.create_all()  # nothing left to do
    assert db.current_revision() == "0001_baseline"


def test_database_from_before_migrations_is_brought_up_to_date(tmp_path):
    db = _pre_migration_db(tmp_path)
    with db.engine.begin() as conn:
        users = Base.metadata.tables["rb_users"]  # model defaults fill the other columns
        conn.execute(users.insert().values(email="a@example.com", name="A", role="admin"))
        # Pretend these came in releases after this database was made.
        conn.execute(text("DROP INDEX ix_rb_api_keys_prefix"))
        conn.execute(text("ALTER TABLE rb_api_keys DROP COLUMN last_used_at"))
        conn.execute(text("DROP TABLE rb_oauth_identities"))
    assert db.current_revision() is None

    db.create_all()
    insp = inspect(db.engine)
    assert "last_used_at" in {c["name"] for c in insp.get_columns("rb_api_keys")}
    assert "ix_rb_api_keys_prefix" in {i["name"] for i in insp.get_indexes("rb_api_keys")}
    assert insp.has_table("rb_oauth_identities")
    with db.engine.connect() as conn:  # existing rows are kept
        assert conn.execute(text("SELECT email FROM rb_users")).scalar() == "a@example.com"
    assert db.current_revision() == "0001_baseline" and db.pending_changes() == []


def test_missing_required_column_stops_with_a_clear_error(tmp_path):
    db = _pre_migration_db(tmp_path)
    with db.engine.begin() as conn:
        conn.execute(text("DROP INDEX ix_rb_api_keys_prefix"))
        conn.execute(text("ALTER TABLE rb_api_keys DROP COLUMN prefix"))
    with pytest.raises(RuntimeError, match="rb_api_keys.prefix is missing and required"):
        db.create_all()
    assert db.current_revision() is None  # nothing recorded; fix it and upgrade again


def test_revision_writes_a_migration_that_applies(tmp_path, monkeypatch):
    migrations = tmp_path / "migrations"
    shutil.copytree(dbmod.MIGRATIONS, migrations)
    monkeypatch.setattr(dbmod, "MIGRATIONS", migrations)
    db = Database(f"sqlite:///{tmp_path}/dev.db")
    db.create_all()
    table = Table(
        "rb_test_notes",
        Base.metadata,
        Column("id", Integer, primary_key=True),
        Column("body", String(200), nullable=True),
    )
    try:
        assert [d[0] for d in db.pending_changes()] == ["add_table"]
        path = db.revision("Add test notes")
        assert path.name == "0002_add_test_notes.py" and path.parent == migrations / "versions"
        script = path.read_text()
        assert "op.create_table('rb_test_notes'" in script
        assert "down_revision = '0001_baseline'" in script
        db.create_all()
        assert db.current_revision() == "0002_add_test_notes" and db.pending_changes() == []
    finally:
        Base.metadata.remove(table)
