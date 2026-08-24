"""FakeGitHub, make_user_with_session, and the payload constants live in conftest.py."""

import time
import uuid
from datetime import datetime

import httpx
from sqlalchemy import func, select

from app.models import ProviderConnection, PullRequest, Repository, Review, UserRepository
from app.services.github_client import GitHubClient


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
    # octocat/beta is private in the fixture and is filtered out: the empty
    # OAuth scope means it should never arrive, and the privacy policy states
    # as fact that private repositories are not touched
    assert [item["full_name"] for item in items] == ["octocat/alpha"]
    assert [item["private"] for item in items] == [False]

    assert db.scalar(select(func.count()).select_from(Repository)) == 1
    assert (
        db.scalar(
            select(func.count())
            .select_from(UserRepository)
            .where(UserRepository.user_id == user.id)
        )
        == 1
    )

    first_synced = {item["full_name"]: item["last_synced_at"] for item in items}

    second = client.get("/repositories")
    assert second.status_code == 200
    resynced = second.json()["items"]
    assert len(resynced) == 1
    assert db.scalar(select(func.count()).select_from(Repository)) == 1
    assert db.scalar(select(func.count()).select_from(UserRepository)) == 1
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
    assert len(response.json()["items"]) == 1
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


# --- GET /repositories/{repo_id} ---


def _synced_repo_id(client) -> str:
    sync = client.get("/repositories")
    assert sync.status_code == 200
    return next(item["id"] for item in sync.json()["items"] if item["full_name"] == "octocat/alpha")


def test_get_repository_without_a_repo_review_answers_null(client, github, make_user_with_session):
    make_user_with_session("alice")
    repo_id = _synced_repo_id(client)

    response = client.get(f"/repositories/{repo_id}")

    assert response.status_code == 200
    body = response.json()
    assert body["id"] == repo_id
    assert body["full_name"] == "octocat/alpha"
    assert body["default_branch"] == "main"
    assert body["latest_repo_review"] is None


def test_get_repository_carries_the_latest_repo_review(client, db, github, make_user_with_session):
    user, _ = make_user_with_session("alice")
    repo_id = _synced_repo_id(client)
    review = Review(
        user_id=user.id,
        repository_id=uuid.UUID(repo_id),
        head_sha="e" * 40,
        base_sha=None,
        status="completed",
    )
    db.add(review)
    db.flush()

    body = client.get(f"/repositories/{repo_id}").json()

    block = body["latest_repo_review"]
    assert block["id"] == str(review.id)
    assert block["status"] == "completed"
    assert block["head_sha"] == "e" * 40
    assert block["created_at"]


def test_another_users_repo_review_stays_out_of_the_block(
    client, db, github, make_user_with_session
):
    """Bob shares the repository, but alice's snapshot review is not his."""
    alice, _ = make_user_with_session("alice")
    repo_id = _synced_repo_id(client)
    db.add(
        Review(
            user_id=alice.id,
            repository_id=uuid.UUID(repo_id),
            head_sha="e" * 40,
            base_sha=None,
            status="completed",
        )
    )
    db.flush()
    bob, _ = make_user_with_session("bob")
    db.add(UserRepository(user_id=bob.id, repository_id=uuid.UUID(repo_id)))
    db.flush()

    body = client.get(f"/repositories/{repo_id}").json()

    assert body["latest_repo_review"] is None


def test_get_repository_foreign_id_indistinguishable_from_missing(
    client, github, make_user_with_session
):
    make_user_with_session("alice")
    repo_id = _synced_repo_id(client)
    make_user_with_session("bob")

    as_bob = client.get(f"/repositories/{repo_id}")
    missing = client.get(f"/repositories/{uuid.uuid4()}")

    assert as_bob.status_code == missing.status_code == 404
    bob_body, missing_body = as_bob.json(), missing.json()
    assert bob_body["error"].pop("request_id")
    assert missing_body["error"].pop("request_id")
    assert bob_body == missing_body


def test_linking_a_repository_twice_is_not_an_error(db, github, make_user_with_session):
    """The race, reduced to the thing that actually breaks.

    sync_user_repositories decides what to link from a snapshot it read
    earlier. When another request commits the same links in between, the
    second insert hits the primary key. Calling the link step twice with the
    same ids is that situation exactly, and it must not raise: with plain ORM
    inserts it raises UniqueViolation, which reached the caller as a 500 on
    the first page a new account ever sees.
    """
    from app.services import repo_service

    user, _ = make_user_with_session("racer")
    with GitHubClient("gho_racer_test_token") as client:
        repos = repo_service.sync_user_repositories(db, user, client)
    repo_ids = [repo.id for repo in repos]
    assert repo_ids

    # The stale snapshot: these are already linked, and we link them again
    repo_service.link_repositories(db, user, repo_ids)
    db.commit()

    linked = db.execute(
        select(func.count()).select_from(UserRepository).where(UserRepository.user_id == user.id)
    ).scalar()
    assert linked == len(repo_ids)  # no duplicates, no error


def test_private_repositories_are_never_listed(client, github, make_user_with_session):
    """The privacy policy states as fact that private repositories are not
    touched, and a repository review ships whole-file contents to an AI
    provider. The empty OAuth scope should make this unreachable; enforcing
    it here is what turns the sentence into a guarantee."""
    make_user_with_session("private-owner")
    assert any(repo["private"] for repo in github.repos), "fixture must contain a private repo"

    listed = client.get("/repositories?sync=true")

    assert listed.status_code == 200
    names = {item["full_name"] for item in listed.json()["items"]}
    private_names = {repo["full_name"] for repo in github.repos if repo["private"]}
    assert names.isdisjoint(private_names)
