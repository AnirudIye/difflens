"""POST /reviews pins a fresh snapshot and enqueues exactly one job.

The partial unique indexes carry the concurrency rules; these tests exercise
them through the API rather than re-testing the schema directly.
"""

import uuid

import httpx
import pytest
from sqlalchemy import select

from app import queue
from app.models import PullRequest, Repository, Review, ReviewJob, UserRepository
from tests.conftest import HEAD_SHA_41, pull_payload


@pytest.fixture(autouse=True)
def doorbell(monkeypatch):
    """Capture doorbell rings instead of touching Redis."""
    rings: list[str] = []
    monkeypatch.setattr(queue, "notify", lambda client, job_id: rings.append(str(job_id)) or True)
    monkeypatch.setattr(queue, "get_redis", lambda: None)
    return rings


@pytest.fixture
def synced_pr(client, github, make_user_with_session, db):
    """Alice with octocat/alpha PR #41 synced; returns (user, pr_row)."""
    user, _ = make_user_with_session("alice")
    sync = client.get("/repositories")
    assert sync.status_code == 200
    repo_id = next(
        item["id"] for item in sync.json()["items"] if item["full_name"] == "octocat/alpha"
    )
    pulls = client.get(f"/repositories/{repo_id}/pull-requests")
    assert pulls.status_code == 200
    pr_id = next(item["id"] for item in pulls.json()["items"] if item["number"] == 41)
    pr = db.get(PullRequest, uuid.UUID(pr_id))
    return user, pr


def test_create_requires_auth(client):
    response = client.post("/reviews", json={"pull_request_id": str(uuid.uuid4())})
    assert response.status_code == 401


def test_create_pins_refetched_snapshot_and_enqueues(client, db, github, synced_pr, doorbell):
    user, pr = synced_pr
    # The branch moved after the sync: the refetched head must win
    moved_head = "f00d" * 10
    github.pulls[0] = pull_payload(41, "Add login form", moved_head)

    response = client.post("/reviews", json={"pull_request_id": str(pr.id)})
    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "queued"
    assert body["head_sha"] == moved_head
    assert body["base_sha"] == pull_payload(41, "", "")["base"]["sha"]

    review = db.get(Review, uuid.UUID(body["id"]))
    assert review is not None and review.user_id == user.id
    job = db.execute(select(ReviewJob).where(ReviewJob.review_id == review.id)).scalar_one()
    assert job.status == "queued"
    # The doorbell rang once, carrying the job id
    assert doorbell == [str(job.id)]
    # The stale PR row caught up with the refetch
    db.refresh(pr)
    assert pr.head_sha == moved_head


def test_duplicate_live_review_conflicts(client, db, synced_pr, github, doorbell):
    _user, pr = synced_pr
    first = client.post("/reviews", json={"pull_request_id": str(pr.id)})
    assert first.status_code == 201

    second = client.post("/reviews", json={"pull_request_id": str(pr.id)})
    assert second.status_code == 409
    body = second.json()["error"]
    assert body["code"] == "review_already_exists"
    assert body["review_id"] == first.json()["id"]

    # No second review or job row appeared, and the doorbell rang only once
    assert db.scalar(select(Review).where(Review.pull_request_id == pr.id)) is not None
    assert len(db.execute(select(Review.id)).all()) == 1
    assert len(db.execute(select(ReviewJob.id)).all()) == 1
    assert len(doorbell) == 1


def test_rerun_allowed_after_failure(client, db, synced_pr, github, doorbell):
    _user, pr = synced_pr
    first = client.post("/reviews", json={"pull_request_id": str(pr.id)})
    assert first.status_code == 201

    review = db.get(Review, uuid.UUID(first.json()["id"]))
    job = db.execute(select(ReviewJob).where(ReviewJob.review_id == review.id)).scalar_one()
    review.status = "failed"
    job.status = "failed"
    db.flush()

    second = client.post("/reviews", json={"pull_request_id": str(pr.id)})
    assert second.status_code == 201
    assert len(db.execute(select(Review.id)).all()) == 2
    assert len(doorbell) == 2


def test_conflict_with_foreign_review_hides_its_id(
    client, db, github, synced_pr, make_user_with_session
):
    # Bob legitimately shares the repo, but Alice's review id is not his to see
    _alice, pr = synced_pr
    first = client.post("/reviews", json={"pull_request_id": str(pr.id)})
    assert first.status_code == 201

    bob, _ = make_user_with_session("bob")
    repo = db.get(Repository, pr.repository_id)
    db.add(UserRepository(user_id=bob.id, repository_id=repo.id))
    db.flush()

    response = client.post("/reviews", json={"pull_request_id": str(pr.id)})
    assert response.status_code == 409
    error = response.json()["error"]
    assert error["code"] == "review_already_exists"
    assert "review_id" not in error


def test_foreign_pr_indistinguishable_from_missing(
    client, github, synced_pr, make_user_with_session
):
    _alice, pr = synced_pr
    make_user_with_session("bob")

    as_bob = client.post("/reviews", json={"pull_request_id": str(pr.id)})
    missing = client.post("/reviews", json={"pull_request_id": str(uuid.uuid4())})

    assert as_bob.status_code == missing.status_code == 404
    bob_body, missing_body = as_bob.json(), missing.json()
    assert bob_body["error"].pop("request_id")
    assert missing_body["error"].pop("request_id")
    assert bob_body == missing_body


def test_closed_pr_conflicts(client, github, synced_pr):
    _user, pr = synced_pr
    github.pulls[0]["state"] = "closed"

    response = client.post("/reviews", json={"pull_request_id": str(pr.id)})
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "pull_request_closed"


def test_github_error_maps_to_502(client, github, synced_pr):
    _user, pr = synced_pr
    github.responses["/repos/octocat/alpha/pulls/41"] = httpx.Response(
        500, json={"message": "boom"}
    )

    response = client.post("/reviews", json={"pull_request_id": str(pr.id)})
    assert response.status_code == 502
    assert response.json()["error"]["code"] == "github_unavailable"


def test_get_review_owner_only(client, db, github, synced_pr, make_user_with_session):
    _alice, pr = synced_pr
    created = client.post("/reviews", json={"pull_request_id": str(pr.id)})
    review_id = created.json()["id"]

    mine = client.get(f"/reviews/{review_id}")
    assert mine.status_code == 200
    body = mine.json()
    assert body["status"] == "queued"
    assert body["head_sha"] == HEAD_SHA_41
    assert body["findings"] == []
    assert body["cancel_requested"] is False

    make_user_with_session("bob")
    as_bob = client.get(f"/reviews/{review_id}")
    missing = client.get(f"/reviews/{uuid.uuid4()}")
    assert as_bob.status_code == missing.status_code == 404
    bob_body, missing_body = as_bob.json(), missing.json()
    assert bob_body["error"].pop("request_id")
    assert missing_body["error"].pop("request_id")
    assert bob_body == missing_body


def test_cancel_queued_review_cancels_immediately(client, db, github, synced_pr):
    _user, pr = synced_pr
    created = client.post("/reviews", json={"pull_request_id": str(pr.id)})
    review_id = created.json()["id"]

    response = client.post(f"/reviews/{review_id}/cancel")
    assert response.status_code == 200
    assert response.json()["status"] == "cancelled"

    review = db.get(Review, uuid.UUID(review_id))
    job = db.execute(select(ReviewJob).where(ReviewJob.review_id == review.id)).scalar_one()
    assert review.status == "cancelled"
    assert job.status == "cancelled"
    assert job.finished_at is not None


def test_cancel_running_review_requests_cancellation(client, db, github, synced_pr):
    _user, pr = synced_pr
    created = client.post("/reviews", json={"pull_request_id": str(pr.id)})
    review_id = created.json()["id"]

    review = db.get(Review, uuid.UUID(review_id))
    job = db.execute(select(ReviewJob).where(ReviewJob.review_id == review.id)).scalar_one()
    review.status = "running"
    job.status = "running"
    db.flush()

    response = client.post(f"/reviews/{review_id}/cancel")
    assert response.status_code == 200
    body = response.json()
    # The worker owns the transition; the API only raises the flag
    assert body["status"] == "running"
    assert body["cancel_requested"] is True

    db.refresh(job)
    assert job.status == "running"
    assert job.cancel_requested is True


def test_cancel_finished_review_conflicts(client, db, github, synced_pr):
    _user, pr = synced_pr
    created = client.post("/reviews", json={"pull_request_id": str(pr.id)})
    review_id = created.json()["id"]

    review = db.get(Review, uuid.UUID(review_id))
    job = db.execute(select(ReviewJob).where(ReviewJob.review_id == review.id)).scalar_one()
    review.status = "completed"
    job.status = "completed"
    db.flush()

    response = client.post(f"/reviews/{review_id}/cancel")
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "review_finished"
