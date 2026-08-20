"""The public demo: what an anonymous visitor can reach, and what they cannot.

The demo is the only unauthenticated surface in the app that starts work, so
the questions here are narrower than "does it render": can it reach GitHub,
can it reach another account's rows, can one visitor spend the service, and
does it disappear completely when it is switched off.
"""

import pytest
import redis
from sqlalchemy import update
from sqlalchemy.exc import IntegrityError

from app import queue
from app.config import settings
from app.demo import sample as demo_sample
from app.demo import service as demo_service
from app.demo.sample import HEAD_SHA, REPO_FULL_NAME
from app.models import ProviderConnection, PullRequest, Repository, Review, ReviewJob, User
from app.rate_limit import KEY_PREFIX
from app.services import review_service
from worker.runner import process_job


@pytest.fixture
def demo_on(monkeypatch):
    monkeypatch.setattr(settings, "demo_mode", True)
    # 0 disables the limiter; the tests that care set their own limit
    monkeypatch.setattr(settings, "demo_rate_limit", 0)

    # Rate-limit counters live in Redis, which no database rollback touches,
    # so without this the limiter tests pass once and then fail for the rest
    # of the window. Cleared on the way in and the way out.
    def clear() -> None:
        try:
            client = queue.get_redis()
            keys = client.keys(f"{KEY_PREFIX}:demo:*")
            if keys:
                client.delete(*keys)
        except redis.RedisError:
            pass  # the limiter fails open anyway; a cleanup that cannot run is not a failure

    clear()
    yield settings
    clear()


@pytest.fixture
def seeded(db, demo_on):
    created = demo_service.seed(db)
    assert created is not None
    return created


def test_routes_are_gone_when_demo_mode_is_off(client, db):
    # 404 rather than 403: there is no surface to probe
    assert client.get("/demo/review").status_code == 404
    assert client.post("/demo/review/rerun").status_code == 404


def test_get_is_404_when_demo_mode_is_on_but_unseeded(client, demo_on):
    response = client.get("/demo/review")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "demo_unavailable"


def test_seed_is_idempotent(db, demo_on):
    first = demo_service.seed(db)
    assert first is not None
    assert demo_service.seed(db) is None
    assert demo_service.seed(db) is None
    repositories = db.query(Repository).filter(Repository.is_demo).all()
    assert len(repositories) == 1
    assert repositories[0].full_name == REPO_FULL_NAME


def test_seed_creates_a_user_with_no_github_token(db, seeded):
    """The demo user holds no token, so the demo path has none to misuse."""
    user = db.query(User).filter(User.github_id == demo_service.DEMO_GITHUB_ID).one()
    connections = db.query(ProviderConnection).filter(ProviderConnection.user_id == user.id).all()
    assert connections == []


def test_only_one_repository_can_be_the_demo(db, seeded):
    db.add(
        Repository(
            github_id=987654,
            full_name="someone/else",
            is_demo=True,
        )
    )
    with pytest.raises(IntegrityError):
        db.flush()
    db.rollback()


def test_demo_review_runs_without_github(db, seeded, github):
    """The whole point: a review with no token, no network, and no cost."""
    _review, job = seeded
    assert process_job(db, job.id, "test-worker") == "completed"
    assert github.calls == [], "the demo path must never call GitHub"


def test_demo_review_completes_with_findings(client, db, seeded):
    _review, job = seeded
    process_job(db, job.id, "test-worker")

    body = client.get("/demo/review").json()
    assert body["status"] == "completed"
    assert body["ai_model"] == "demo"
    assert body["findings_count"] and body["findings_count"] > 0
    assert body["pull_request"]["repository_full_name"] == REPO_FULL_NAME
    assert any(finding["source"] == "hybrid" for finding in body["findings"])


def test_demo_findings_carry_no_feedback_verdicts(client, db, seeded):
    _review, job = seeded
    process_job(db, job.id, "test-worker")
    body = client.get("/demo/review").json()
    assert all(finding["feedback"] is None for finding in body["findings"])


def test_get_returns_the_demo_review_not_someone_elses(
    client, db, seeded, make_user_with_session, github
):
    """A real user's review exists; the demo route must still answer the demo's."""
    _review, job = seeded
    process_job(db, job.id, "test-worker")

    user, _token = make_user_with_session("octocat")
    other_repo = Repository(github_id=4242, full_name="octocat/private-thing")
    db.add(other_repo)
    db.flush()
    other_pull = PullRequest(
        repository_id=other_repo.id,
        github_number=7,
        title="Secret work",
        state="open",
        head_sha="f" * 40,
    )
    db.add(other_pull)
    db.flush()
    db.add(
        Review(
            user_id=user.id,
            pull_request_id=other_pull.id,
            head_sha="f" * 40,
            base_sha="e" * 40,
            status="completed",
        )
    )
    db.flush()

    body = client.get("/demo/review").json()
    assert body["pull_request"]["repository_full_name"] == REPO_FULL_NAME
    assert body["pull_request"]["title"] != "Secret work"


def test_rerun_supersedes_and_queues_a_fresh_review(client, db, seeded):
    original, job = seeded
    process_job(db, job.id, "test-worker")

    response = client.post("/demo/review/rerun")
    assert response.status_code == 201
    assert response.json()["status"] == "queued"

    db.expire_all()
    assert db.get(Review, original.id).status == "superseded"
    assert client.get("/demo/review").json()["id"] == response.json()["id"]


def test_rerun_refuses_while_a_review_is_live(client, db, seeded):
    response = client.post("/demo/review/rerun")
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "demo_review_running"


def test_rerun_is_404_when_demo_mode_is_off(client, db, seeded, monkeypatch):
    monkeypatch.setattr(settings, "demo_mode", False)
    assert client.post("/demo/review/rerun").status_code == 404


def test_rerun_is_ip_rate_limited(client, db, seeded, monkeypatch):
    original, job = seeded
    process_job(db, job.id, "test-worker")
    monkeypatch.setattr(settings, "demo_rate_limit", 1)
    monkeypatch.setattr(settings, "demo_rate_limit_window_s", 3600)

    headers = {"X-Forwarded-For": "203.0.113.9"}
    first = client.post("/demo/review/rerun", headers=headers)
    assert first.status_code == 201

    # Finish it so the 409 path cannot mask the 429 we are testing for
    queued = db.query(Review).filter(Review.status == "queued").one()
    job_row = review_service.latest_job(db, queued)
    assert job_row is not None
    process_job(db, job_row.id, "test-worker")

    second = client.post("/demo/review/rerun", headers=headers)
    assert second.status_code == 429
    assert second.json()["error"]["code"] == "rate_limited"
    assert second.headers["Retry-After"]


def test_rate_limit_counts_forwarded_addresses_separately(client, db, seeded, monkeypatch):
    original, job = seeded
    process_job(db, job.id, "test-worker")
    monkeypatch.setattr(settings, "demo_rate_limit", 1)

    assert (
        client.post("/demo/review/rerun", headers={"X-Forwarded-For": "198.51.100.1"}).status_code
        == 201
    )
    # A different address is a different bucket, so this is refused by the
    # live-review index rather than by the limiter
    other = client.post("/demo/review/rerun", headers={"X-Forwarded-For": "198.51.100.2"})
    assert other.status_code == 409


def test_prune_keeps_only_recent_history(db, seeded):
    user, _repository, pull = demo_service.demo_context(db)
    for index in range(6):
        db.add(
            Review(
                user_id=user.id,
                pull_request_id=pull.id,
                head_sha=HEAD_SHA,
                base_sha="0" * 40,
                status="superseded",
                findings_count=index,
            )
        )
    db.flush()

    removed = demo_service.prune(db, keep=3)
    assert removed == 3

    # keep counts finished reviews; the queued one the fixture seeded is
    # excluded from the candidate set rather than competing for a slot
    finished = (
        db.query(Review)
        .filter(Review.pull_request_id == pull.id, Review.status == "superseded")
        .count()
    )
    assert finished == 3
    total = db.query(Review).filter(Review.pull_request_id == pull.id).count()
    assert total == 4


def test_prune_never_removes_a_live_review(db, seeded):
    live, _job = seeded
    user, _repository, pull = demo_service.demo_context(db)
    for _ in range(5):
        db.add(
            Review(
                user_id=user.id,
                pull_request_id=pull.id,
                head_sha=HEAD_SHA,
                base_sha="0" * 40,
                status="superseded",
            )
        )
    db.flush()

    demo_service.prune(db, keep=1)
    assert db.get(Review, live.id) is not None


def test_current_review_is_none_without_a_demo_repository(db):
    assert demo_service.current_review(db) is None


def test_demo_context_raises_when_unseeded(db):
    with pytest.raises(demo_service.DemoNotSeeded):
        demo_service.demo_context(db)


def test_cancelling_a_demo_review_reports_cancelled_not_lost(db, seeded, monkeypatch):
    """The outcome string has to name what actually happened.

    An earlier shape checkpointed inside the demo runner and signalled early
    exit by returning None, so the caller asked _checkpoint again; cancel_job
    had already moved the row out of "running" by then, and the cancellation
    came back as "lost", which is the answer for a job someone else took.
    """
    review, job = seeded
    real_populate = demo_sample.populate_workspace

    def cancel_midway(workspace):
        # Raise the flag at the moment the workspace is materialized, which
        # is the window a real cancel would land in
        db.execute(update(ReviewJob).where(ReviewJob.id == job.id).values(cancel_requested=True))
        db.commit()
        return real_populate(workspace)

    monkeypatch.setattr(demo_sample, "populate_workspace", cancel_midway)

    # The flag is raised before the run starts here, so the checkpoint that
    # guards the demo branch is the one that sees it
    db.execute(update(ReviewJob).where(ReviewJob.id == job.id).values(cancel_requested=True))
    db.commit()

    assert process_job(db, job.id, "test-worker") == "cancelled"
    db.expire_all()
    assert db.get(Review, review.id).status == "cancelled"


def test_a_cancelled_demo_review_can_be_run_again(client, db, seeded):
    """A cancelled run must not wedge the demo: cancelled sits outside the
    live index, so the next visitor can start a fresh one."""
    review, job = seeded
    db.execute(update(ReviewJob).where(ReviewJob.id == job.id).values(cancel_requested=True))
    db.commit()
    assert process_job(db, job.id, "test-worker") == "cancelled"

    response = client.post("/demo/review/rerun")
    assert response.status_code == 201
    assert response.json()["status"] == "queued"
