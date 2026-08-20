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


def test_create_response_carries_pull_context(client, synced_pr, doorbell):
    """The review page renders its header from this block; dropping it from
    the create response would blank the page the user lands on."""
    _user, pr = synced_pr
    body = client.post("/reviews", json={"pull_request_id": str(pr.id)}).json()
    pull = body["pull_request"]
    assert pull["id"] == str(pr.id)
    assert pull["number"] == 41
    assert pull["title"] == "Add login form"
    assert pull["repository_full_name"] == "octocat/alpha"
    assert uuid.UUID(pull["repository_id"])
    assert body["findings"] == []


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


def _finish(db, review_id: uuid.UUID, status: str = "completed"):
    """Land a review and its job in a terminal state, as the worker would."""
    review = db.get(Review, review_id)
    job = db.execute(select(ReviewJob).where(ReviewJob.review_id == review.id)).scalar_one()
    review.status = status
    job.status = status if status != "superseded" else "completed"
    db.flush()
    return review


def test_rerun_supersedes_the_finished_review(client, db, synced_pr, github, doorbell):
    """Re-reviewing the same commit is the whole point: nothing about the PR
    changed, the reviewer's configuration did."""
    _user, pr = synced_pr
    first = client.post("/reviews", json={"pull_request_id": str(pr.id)}).json()
    old = _finish(db, uuid.UUID(first["id"]))

    response = client.post(f"/reviews/{first['id']}/rerun")

    assert response.status_code == 201
    fresh = response.json()
    assert fresh["id"] != first["id"]
    assert fresh["status"] == "queued"
    # Same commit as before: this is a re-review, not a new snapshot
    assert fresh["head_sha"] == first["head_sha"]
    assert fresh["pull_request"]["number"] == 41
    db.refresh(old)
    assert old.status == "superseded"
    assert len(db.execute(select(Review.id)).all()) == 2
    assert len(doorbell) == 2


def test_rerun_of_a_closed_pull_request_leaves_the_old_review_alone(
    client, db, synced_pr, github, doorbell
):
    """A refused rerun must refuse completely.

    The supersede used to happen first, and create_review commits on the
    closed path, so the commit took the pending supersede with it: the API
    answered 409, and the old review was left superseded with nothing
    replacing it. The page then told the user a newer review had replaced
    this one, which was a specific falsehood.
    """
    _user, pr = synced_pr
    first = client.post("/reviews", json={"pull_request_id": str(pr.id)}).json()
    old = _finish(db, uuid.UUID(first["id"]))
    github.pulls[0] = {**pull_payload(41, "Add login form", first["head_sha"]), "state": "closed"}

    response = client.post(f"/reviews/{first['id']}/rerun")

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "pull_request_closed"
    db.refresh(old)
    assert old.status == "completed", "a refused rerun still moved the old review"
    assert len(db.execute(select(Review.id)).all()) == 1


@pytest.mark.parametrize("status", ["failed", "cancelled"])
def test_rerun_after_a_failure_keeps_the_failure_on_the_record(
    client, db, synced_pr, github, doorbell, status
):
    """Only a completed review needs superseding.

    Failed and cancelled reviews sit outside uq_reviews_pr_sha_live already,
    so overwriting their status buys nothing and costs the record of how they
    ended: the review page renders superseded as a finished pass, so a failed
    review would come back as a clean one with its error message gone.
    """
    _user, pr = synced_pr
    first = client.post("/reviews", json={"pull_request_id": str(pr.id)}).json()
    old = _finish(db, uuid.UUID(first["id"]), status)
    old.error_user_message = "GitHub did not respond as expected"
    db.flush()

    fresh = client.post(f"/reviews/{first['id']}/rerun")

    assert fresh.status_code == 201
    db.refresh(old)
    assert old.status == status
    body = client.get(f"/reviews/{first['id']}").json()
    assert body["status"] == status
    assert body["error_user_message"] == "GitHub did not respond as expected"


def test_rerun_refuses_while_the_review_is_live(client, db, synced_pr, github, doorbell):
    _user, pr = synced_pr
    first = client.post("/reviews", json={"pull_request_id": str(pr.id)}).json()

    response = client.post(f"/reviews/{first['id']}/rerun")

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "review_still_running"
    db.refresh(db.get(Review, uuid.UUID(first["id"])))
    assert db.get(Review, uuid.UUID(first["id"])).status == "queued"
    assert len(db.execute(select(Review.id)).all()) == 1
    assert len(doorbell) == 1


def test_rerun_picks_up_a_moved_head(client, db, synced_pr, github, doorbell):
    _user, pr = synced_pr
    first = client.post("/reviews", json={"pull_request_id": str(pr.id)}).json()
    _finish(db, uuid.UUID(first["id"]))
    moved_head = "f00d" * 10
    github.pulls[0] = pull_payload(41, "Add login form", moved_head)

    fresh = client.post(f"/reviews/{first['id']}/rerun").json()

    assert fresh["head_sha"] == moved_head


def test_rerun_is_owned(client, db, synced_pr, github, make_user_with_session, doorbell):
    _alice, pr = synced_pr
    first = client.post("/reviews", json={"pull_request_id": str(pr.id)}).json()
    _finish(db, uuid.UUID(first["id"]))
    _bob, bob_token = make_user_with_session("bob")
    client.cookies.set("session", bob_token)

    response = client.post(f"/reviews/{first['id']}/rerun")

    assert response.status_code == 404
    assert len(db.execute(select(Review.id)).all()) == 1


def test_rerun_requires_auth(client):
    assert client.post(f"/reviews/{uuid.uuid4()}/rerun").status_code == 401


def test_superseded_review_is_still_readable(client, db, synced_pr, github, doorbell):
    """History must survive: the old review keeps its findings and stays
    reachable by the link the user may already have."""
    _user, pr = synced_pr
    first = client.post("/reviews", json={"pull_request_id": str(pr.id)}).json()
    _finish(db, uuid.UUID(first["id"]))
    client.post(f"/reviews/{first['id']}/rerun")

    body = client.get(f"/reviews/{first['id']}")

    assert body.status_code == 200
    assert body.json()["status"] == "superseded"


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
