import itertools
import time
import uuid
from datetime import datetime

import httpx
import pytest
from sqlalchemy import func, select

from app import security
from app.models import ProviderConnection, PullRequest, Repository, User, UserRepository

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


def _pull_payload(number, title, head_sha):
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
            _pull_payload(41, "Add login form", HEAD_SHA_41),
            _pull_payload(42, "Fix flaky sync", HEAD_SHA_42),
        ]
        self.responses: dict[str, httpx.Response] = {}

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


def test_list_repositories_requires_auth(client):
    response = client.get("/repositories")
    assert response.status_code == 401
    error = response.json()["error"]
    assert error["code"] == "unauthenticated"
    assert set(error) == {"code", "message", "request_id"}


def test_sync_persists_repositories_and_is_idempotent(client, db, github, make_user_with_session):
    user, _ = make_user_with_session("alice")

    first = client.get("/repositories")
    assert first.status_code == 200
    items = first.json()["items"]
    assert [item["full_name"] for item in items] == ["octocat/alpha", "octocat/beta"]
    assert [item["private"] for item in items] == [False, True]

    assert db.scalar(select(func.count()).select_from(Repository)) == 2
    assert (
        db.scalar(
            select(func.count())
            .select_from(UserRepository)
            .where(UserRepository.user_id == user.id)
        )
        == 2
    )

    first_synced = {item["full_name"]: item["last_synced_at"] for item in items}

    second = client.get("/repositories")
    assert second.status_code == 200
    resynced = second.json()["items"]
    assert len(resynced) == 2
    assert db.scalar(select(func.count()).select_from(Repository)) == 2
    assert db.scalar(select(func.count()).select_from(UserRepository)) == 2
    for item in resynced:
        assert datetime.fromisoformat(item["last_synced_at"]) > datetime.fromisoformat(
            first_synced[item["full_name"]]
        )


def test_sync_false_serves_linked_rows_without_github(client, github, make_user_with_session):
    make_user_with_session("alice")
    assert client.get("/repositories").status_code == 200
    github.calls.clear()

    response = client.get("/repositories", params={"sync": "false"})
    assert response.status_code == 200
    assert len(response.json()["items"]) == 2
    assert github.calls == []


def test_other_users_repo_is_indistinguishable_from_missing(client, github, make_user_with_session):
    make_user_with_session("alice")
    sync = client.get("/repositories")
    assert sync.status_code == 200
    alice_repo_id = sync.json()["items"][0]["id"]

    make_user_with_session("bob")
    as_bob = client.get(f"/repositories/{alice_repo_id}/pull-requests")
    missing = client.get(f"/repositories/{uuid.uuid4()}/pull-requests")

    assert as_bob.status_code == missing.status_code == 404
    bob_body = as_bob.json()
    missing_body = missing.json()
    assert bob_body["error"].pop("request_id")
    assert missing_body["error"].pop("request_id")
    assert bob_body == missing_body


def test_pull_requests_upsert_for_owner(client, db, github, make_user_with_session):
    make_user_with_session("alice")
    sync = client.get("/repositories")
    assert sync.status_code == 200
    repo_id = next(
        item["id"] for item in sync.json()["items"] if item["full_name"] == "octocat/alpha"
    )

    first = client.get(f"/repositories/{repo_id}/pull-requests")
    assert first.status_code == 200
    assert sorted(item["number"] for item in first.json()["items"]) == [41, 42]

    def rows_by_number():
        return {
            pull.github_number: pull
            for pull in db.execute(
                select(PullRequest).where(PullRequest.repository_id == uuid.UUID(repo_id))
            ).scalars()
        }

    assert set(rows_by_number()) == {41, 42}

    new_sha = "1234567890abcdef1234567890abcdef12345678"
    github.pulls[0]["title"] = "Add login form with validation"
    github.pulls[0]["head"]["sha"] = new_sha

    second = client.get(f"/repositories/{repo_id}/pull-requests")
    assert second.status_code == 200
    updated = rows_by_number()
    assert set(updated) == {41, 42}
    assert updated[41].title == "Add login form with validation"
    assert updated[41].head_sha == new_sha


def test_invalid_token_maps_to_reconnect_required(client, db, github, make_user_with_session):
    user, _ = make_user_with_session("alice")
    connection = db.execute(
        select(ProviderConnection).where(ProviderConnection.user_id == user.id)
    ).scalar_one()
    connection.token_invalid = True
    db.flush()

    response = client.get("/repositories")
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "github_reconnect_required"
    assert github.calls == []


def test_rate_limit_maps_to_503_with_retry_after(client, github, make_user_with_session):
    make_user_with_session("alice")
    github.responses["/user/repos"] = httpx.Response(
        403,
        headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": str(int(time.time()) + 90)},
        json={"message": "API rate limit exceeded"},
    )

    response = client.get("/repositories")
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "github_rate_limited"
    assert 1 <= int(response.headers["Retry-After"]) <= 90


def test_github_server_error_maps_to_502(client, github, make_user_with_session):
    make_user_with_session("alice")
    github.responses["/user/repos"] = httpx.Response(500, json={"message": "boom"})

    response = client.get("/repositories")
    assert response.status_code == 502
    assert response.json()["error"]["code"] == "github_unavailable"
