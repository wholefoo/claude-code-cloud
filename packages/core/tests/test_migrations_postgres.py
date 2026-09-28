"""Migrations against a real Postgres (the CI ``postgres`` job sets RB_TEST_POSTGRES_URL).

Each test wipes the ``public`` schema, so the database name must contain "test"."""

from __future__ import annotations

import os
import threading

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url

from redblue.core import db as dbmod
from redblue.core.db import Base, Database

URL = os.environ.get("RB_TEST_POSTGRES_URL", "")
pytestmark = pytest.mark.skipif(not URL, reason="set RB_TEST_POSTGRES_URL to run")


@pytest.fixture
def url():
    if "test" not in (make_url(URL).database or ""):
        pytest.fail("RB_TEST_POSTGRES_URL must point at a database whose name contains 'test'")
    engine = create_engine(URL)
    with engine.begin() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE"))
        conn.execute(text("CREATE SCHEMA public"))
    engine.dispose()
    return URL


def _pre_migration_db(url: str) -> Database:
    db = Database(url)
    dbmod._import_models()
    Base.metadata.create_all(db.engine)
    return db


def test_fresh_database_matches_the_models(url):
    db = Database(url)
    db.create_all()
    assert db.current_revision() == "0001_baseline"
    assert db.pending_changes() == []
    db.create_all()
    assert db.current_revision() == "0001_baseline"


def test_database_from_before_migrations_is_brought_up_to_date(url):
    db = _pre_migration_db(url)
    with db.engine.begin() as conn:
        users = Base.metadata.tables["rb_users"]
        conn.execute(users.insert().values(email="a@example.com", name="A", role="admin"))
        conn.execute(text("DROP INDEX ix_rb_api_keys_prefix"))
        conn.execute(text("ALTER TABLE rb_api_keys DROP COLUMN last_used_at"))
        conn.execute(text("DROP TABLE rb_oauth_identities"))

    db.create_all()
    insp = inspect(db.engine)
    assert "last_used_at" in {c["name"] for c in insp.get_columns("rb_api_keys")}
    assert "ix_rb_api_keys_prefix" in {i["name"] for i in insp.get_indexes("rb_api_keys")}
    assert insp.has_table("rb_oauth_identities")
    with db.engine.connect() as conn:
        assert conn.execute(text("SELECT email FROM rb_users")).scalar() == "a@example.com"
    assert db.current_revision() == "0001_baseline" and db.pending_changes() == []


def test_failed_upgrade_changes_nothing(url):
    """Postgres runs DDL in the transaction: a failed baseline leaves no half-upgrade."""
    db = _pre_migration_db(url)
    with db.engine.begin() as conn:
        conn.execute(text("DROP TABLE rb_oauth_identities"))  # would be recreated...
        conn.execute(text("DROP INDEX ix_rb_api_keys_prefix"))
        conn.execute(text("ALTER TABLE rb_api_keys DROP COLUMN prefix"))  # ...but this stops it
    with pytest.raises(RuntimeError, match="rb_api_keys.prefix is missing and required"):
        db.create_all()
    assert db.current_revision() is None
    assert not inspect(db.engine).has_table("rb_oauth_identities")  # rolled back


def test_workers_starting_together_upgrade_once(url):
    """Several processes starting at once: the advisory lock serializes the upgrades."""
    errors: list[BaseException] = []

    def start():
        try:
            Database(url).create_all()
        except BaseException as exc:  # noqa: BLE001 - reported below
            errors.append(exc)

    threads = [threading.Thread(target=start) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=120)
    assert errors == []
    with create_engine(url).connect() as conn:
        rows = conn.execute(text("SELECT version_num FROM alembic_version")).all()
    assert rows == [("0001_baseline",)]
