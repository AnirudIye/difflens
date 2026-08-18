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
# Redis db 1 so tests never drain a dev worker's doorbell on db 0
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/1")

import base64
import itertools
import re
from pathlib import Path

import httpx
import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from app import security
from app.db import get_db
from app.main import app
from app.models import ProviderConnection, User

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


REPOS_PAYLOAD = [
    {
        "id": 9101,
        "name": "alpha",
        "full_name": "octocat/alpha",
        "owner": {"login": "octocat"},
        "private": False,
        "default_branch": "main",
        "html_url": "https://github.com/octocat/alpha",
    },
    {
        "id": 9102,
        "name": "beta",
        "full_name": "octocat/beta",
        "owner": {"login": "octocat"},
        "private": True,
        "default_branch": "develop",
        "html_url": "https://github.com/octocat/beta",
    },
]

BASE_SHA = "c1a6de5d4a4a1c2a5c1e0b6f3d8e9a7b5c4d3e2f"
HEAD_SHA_41 = "0f1e2d3c4b5a69788796a5b4c3d2e1f0aabbccdd"
HEAD_SHA_42 = "ddccbbaa0f1e2d3c4b5a69788796a5b4c3d2e1f0"


def pull_payload(number, title, head_sha):
    return {
        "id": 88000 + number,
        "number": number,
        "title": title,
        "state": "open",
        "user": {"login": "octocat"},
        "base": {"ref": "main", "sha": BASE_SHA},
        "head": {"ref": f"feature-{number}", "sha": head_sha},
        "html_url": f"https://github.com/octocat/alpha/pull/{number}",
        "updated_at": "2026-08-16T09:30:00Z",
    }


class FakeGitHub:
    def __init__(self):
        self.calls: list[httpx.Request] = []
        self.repos = REPOS_PAYLOAD
        self.pulls = [
            pull_payload(41, "Add login form", HEAD_SHA_41),
            pull_payload(42, "Fix flaky sync", HEAD_SHA_42),
        ]
        self.responses: dict[str, httpx.Response] = {}
        # (full_name, base_sha, head_sha) -> compare payload; (path, ref) -> bytes
        self.compares: dict[tuple[str, str, str], dict] = {}
        self.contents: dict[tuple[str, str], bytes] = {}

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        assert request.headers["Authorization"].startswith("Bearer gho_")
        assert request.headers["X-GitHub-Api-Version"] == "2022-11-28"
        path = request.url.path
        if path in self.responses:
            return self.responses[path]
        if path == "/user/repos":
            return httpx.Response(200, json=self.repos)
        if path.startswith("/repos/") and path.endswith("/pulls"):
            return httpx.Response(200, json=self.pulls)
        single = re.fullmatch(r"/repos/[^/]+/[^/]+/pulls/(\d+)", path)
        if single:
            number = int(single.group(1))
            for pull in self.pulls:
                if pull["number"] == number:
                    return httpx.Response(200, json=pull)
            return httpx.Response(404, json={"message": "Not Found"})
        compare = re.fullmatch(r"/repos/([^/]+/[^/]+)/compare/([^.]+)\.\.\.(.+)", path)
        if compare:
            payload = self.compares.get((compare.group(1), compare.group(2), compare.group(3)))
            if payload is not None:
                return httpx.Response(200, json=payload)
            return httpx.Response(404, json={"message": "Not Found"})
        content = re.fullmatch(r"/repos/[^/]+/[^/]+/contents/(.+)", path)
        if content:
            ref = request.url.params.get("ref", "")
            blob = self.contents.get((content.group(1), ref))
            if blob is not None:
                return httpx.Response(
                    200,
                    json={"encoding": "base64", "content": base64.b64encode(blob).decode()},
                )
            return httpx.Response(404, json={"message": "Not Found"})
        return httpx.Response(404, json={"message": "Not Found"})


@pytest.fixture
def github(monkeypatch):
    fake = FakeGitHub()
    real_client = httpx.Client
    transport = httpx.MockTransport(fake.handler)
    # TestClient subclassed httpx.Client before this patch, so only the app's
    # outbound GitHub client is rerouted; kwargs must pass through or
    # GitHubClient loses its base_url and auth headers.
    monkeypatch.setattr(
        httpx, "Client", lambda **kwargs: real_client(transport=transport, **kwargs)
    )
    return fake


@pytest.fixture
def make_user_with_session(db, client):
    github_ids = itertools.count(510001)

    def _make(login: str) -> tuple[User, str]:
        github_id = next(github_ids)
        user = User(github_id=github_id, login=login)
        db.add(user)
        db.flush()
        db.add(
            ProviderConnection(
                user_id=user.id,
                provider_account_id=github_id,
                access_token_enc=security.encrypt_token(f"gho_{login}_test_token"),
            )
        )
        token = security.mint_session(db, user.id)
        db.flush()
        # The last caller owns the cookie; switch users by re-setting the returned token
        client.cookies.set("session", token)
        return user, token

    return _make
