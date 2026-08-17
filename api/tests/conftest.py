"""Shared fixtures: a migrated Postgres test database and a transactional test client.

The environment is pinned before any app import because app.config builds its
Settings singleton and app.db builds its engine at import time.
"""

import os

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+psycopg://difflens:difflens@localhost:55432/difflens_test",
)

os.environ["DATABASE_URL"] = TEST_DATABASE_URL
os.environ["GITHUB_CLIENT_ID"] = "test-client-id"
os.environ["SESSION_SECRET"] = "test-session-secret"
os.environ["TOKEN_ENCRYPTION_KEY"] = "test-token-encryption-key"

from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from app.db import get_db
from app.main import app

API_DIR = Path(__file__).resolve().parents[1]


def _create_test_database_if_missing() -> None:
    url = make_url(TEST_DATABASE_URL)
    admin = create_engine(url.set(database="postgres"), isolation_level="AUTOCOMMIT")
    with admin.connect() as connection:
        exists = connection.execute(
            text("SELECT 1 FROM pg_database WHERE datname = :name"),
            {"name": url.database},
        ).scalar()
        if not exists:
            connection.execute(text(f'CREATE DATABASE "{url.database}"'))
    admin.dispose()


@pytest.fixture(scope="session")
def engine():
    _create_test_database_if_missing()
    config = Config(str(API_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(API_DIR / "alembic"))
    command.upgrade(config, "head")
    engine = create_engine(TEST_DATABASE_URL)
    yield engine
    engine.dispose()


@pytest.fixture
def db(engine):
    # Each test runs inside one connection-level transaction that is rolled
    # back at teardown; create_savepoint turns the app's commit() calls into
    # savepoint releases so the final rollback still wipes everything.
    connection = engine.connect()
    outer = connection.begin()
    session = Session(bind=connection, join_transaction_mode="create_savepoint")
    yield session
    session.close()
    outer.rollback()
    connection.close()


@pytest.fixture
def client(db):
    def override_get_db():
        yield db

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.pop(get_db, None)
