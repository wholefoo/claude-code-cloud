import pytest

from redblue.core.auth import Role, create_user
from redblue.core.db import Database


@pytest.fixture
def db():
    d = Database("sqlite://")
    d.create_all()
    with d.session() as s:
        yield s


@pytest.fixture
def users(db):
    return {r: create_user(db, f"{r.name}@example.com", "password-1234", r) for r in Role}
