"""Review creation at the seam the API cannot reach.

Everything else about creating a review is exercised through the routers in
test_reviews_api.py. These tests call the service directly, because the
behaviour under test lives in a window inside one function and a request has
no way to land in the middle of it.
"""

import pytest
import structlog
from sqlalchemy import select, update

from app.models import PullRequest, Repository, Review, ReviewJob, User
from app.services import review_service

BASE_SHA = "b" * 40


@pytest.fixture
def pull(db):
    """One user with one open pull request, built without touching GitHub."""
    user = User(github_id=771001, login="racer")
    repository = Repository(github_id=771002, full_name="difflens/race")
    db.add_all([user, repository])
    db.flush()
    pull_request = PullRequest(
        repository_id=repository.id,
        github_number=3,
        title="Land the thing",
        state="open",
        head_sha="c" * 40,
    )
    db.add(pull_request)
    db.flush()
    return user, pull_request


def _supersede_during_recovery(db, monkeypatch, winner_id) -> list[bool]:
    """Move the winning review out of the live set as the recovery select runs.

    insert_review has already rolled back by the time it builds that select,
    so the session is usable here. This is the exact window the real race
    lands in: the index has refused the INSERT, and the row that caused the
    refusal is gone before anyone can read it back.

    Anchored on the session having no pending insert rather than on being the
    first select to run. `db.add(review)` happens before the flush, so any
    select between the add and the failed flush still has that Review in
    `db.new`, and only the post-rollback recovery select sees it empty. Firing
    on call order instead would silently relocate this hook to any select
    added earlier in insert_review, and the test would keep passing while
    exercising nothing.
    """
    real_select = review_service.select
    fired: list[bool] = []

    def superseding_select(*args, **kwargs):
        if not fired and not db.new:
            fired.append(True)
            db.execute(update(Review).where(Review.id == winner_id).values(status="superseded"))
        return real_select(*args, **kwargs)

    monkeypatch.setattr(review_service, "select", superseding_select)
    return fired


def test_conflict_survives_the_winner_leaving_the_live_set(db, pull, monkeypatch):
    """The read-back that names the winning review can come up empty.

    insert_review learns that a conflict happened from the partial unique
    index, then selects the live review so the conflict can name it. If that
    review leaves the live statuses in between, the select matches nothing,
    and the raw IntegrityError used to escape from there as a 500.

    The index is still the authority on what happened: a live review existed
    when the INSERT ran. So the answer is the same conflict, carrying no
    review id because there is no longer a row to point the caller at.
    """
    user, pull_request = pull
    winner, _job = review_service.insert_review(
        db, user, pull_request, pull_request.head_sha, BASE_SHA
    )
    fired = _supersede_during_recovery(db, monkeypatch, winner.id)

    with pytest.raises(review_service.ReviewAlreadyExists) as excinfo:
        review_service.insert_review(db, user, pull_request, pull_request.head_sha, BASE_SHA)

    assert fired, "the recovery select never ran, so the race was not exercised"
    assert excinfo.value.review_id is None
    # The losing INSERT left nothing behind
    assert len(db.execute(select(Review.id)).all()) == 1
    assert len(db.execute(select(ReviewJob.id)).all()) == 1


def test_the_winner_still_live_is_named_as_before(db, pull):
    """The ordinary conflict is unchanged: the caller's own review is named."""
    user, pull_request = pull
    winner, _job = review_service.insert_review(
        db, user, pull_request, pull_request.head_sha, BASE_SHA
    )

    with pytest.raises(review_service.ReviewAlreadyExists) as excinfo:
        review_service.insert_review(db, user, pull_request, pull_request.head_sha, BASE_SHA)

    assert excinfo.value.review_id == winner.id


def test_an_unnamed_constraint_is_not_treated_as_a_conflict(db, pull, monkeypatch):
    """A driver that does not name the constraint gives None, not a match.

    None means "unknown", and answering a conflict on unknown would let a
    genuine integrity bug masquerade as a lost race forever.
    """
    user, pull_request = pull
    winner, _job = review_service.insert_review(
        db, user, pull_request, pull_request.head_sha, BASE_SHA
    )
    _supersede_during_recovery(db, monkeypatch, winner.id)
    monkeypatch.setattr(review_service, "_violated_constraint", lambda exc: None)

    with pytest.raises(review_service.IntegrityError):
        review_service.insert_review(db, user, pull_request, pull_request.head_sha, BASE_SHA)


def test_violated_constraint_reads_the_index_postgres_named(db, pull):
    """The helper reports the real index name from a real violation."""
    user, pull_request = pull
    review_service.insert_review(db, user, pull_request, pull_request.head_sha, BASE_SHA)

    duplicate = Review(
        user_id=user.id,
        pull_request_id=pull_request.id,
        head_sha=pull_request.head_sha,
        base_sha=BASE_SHA,
        status="queued",
    )
    db.add(duplicate)
    with pytest.raises(review_service.IntegrityError) as excinfo:
        db.flush()
    db.rollback()

    assert review_service._violated_constraint(excinfo.value) == review_service.LIVE_REVIEW_INDEX


@pytest.mark.parametrize("constraint", ["uq_jobs_one_live_per_review", "uq_pull_requests_x", None])
def test_only_the_live_review_index_is_answered_as_a_conflict(db, pull, monkeypatch, constraint):
    """Anything the live-review index did not refuse is a bug, not a race.

    `db.commit()` flushes the whole session, so a refetched pull request or a
    superseded review can be what Postgres actually refused. Classifying on
    "is there a live review" rather than on the constraint would dress any of
    those as a 409 telling the caller to wait for a review that does exist,
    and it would do it with nothing in the log. uq_jobs_one_live_per_review
    belongs in the same bucket: it cannot lose a race, because reviews.id is
    a fresh gen_random_uuid on every call.
    """
    user, pull_request = pull
    review_service.insert_review(db, user, pull_request, pull_request.head_sha, BASE_SHA)
    monkeypatch.setattr(review_service, "_violated_constraint", lambda exc: constraint)

    with structlog.testing.capture_logs() as logs:
        with pytest.raises(review_service.IntegrityError):
            review_service.insert_review(db, user, pull_request, pull_request.head_sha, BASE_SHA)

    errored = [e for e in logs if e["event"] == "review_insert_failed_unexpectedly"]
    assert errored, "an unexpected integrity error surfaced with nothing in the log"
    assert errored[0]["constraint"] == constraint


def test_a_real_conflict_is_logged_either_way(db, pull, monkeypatch):
    """Both conflict paths log, and say whether the winner was still there."""
    user, pull_request = pull
    winner, _job = review_service.insert_review(
        db, user, pull_request, pull_request.head_sha, BASE_SHA
    )

    with structlog.testing.capture_logs() as logs:
        with pytest.raises(review_service.ReviewAlreadyExists):
            review_service.insert_review(db, user, pull_request, pull_request.head_sha, BASE_SHA)
    still_live = [e for e in logs if e["event"] == "review_insert_conflict"]
    assert still_live and still_live[0]["winner_still_live"] is True

    _supersede_during_recovery(db, monkeypatch, winner.id)
    with structlog.testing.capture_logs() as logs:
        with pytest.raises(review_service.ReviewAlreadyExists):
            review_service.insert_review(db, user, pull_request, pull_request.head_sha, BASE_SHA)
    vanished = [e for e in logs if e["event"] == "review_insert_conflict"]
    assert vanished and vanished[0]["winner_still_live"] is False


def test_the_job_row_lands_with_its_review(db, pull):
    """Moving the job insert inside the try must not change the happy path.

    reviews.id is a server default, so the job can only be built after the
    flush; building it earlier would silently write review_id NULL.
    """
    user, pull_request = pull
    review, job = review_service.insert_review(
        db, user, pull_request, pull_request.head_sha, BASE_SHA
    )

    assert job.review_id == review.id
    assert review.id is not None
    stored = db.execute(select(ReviewJob).where(ReviewJob.review_id == review.id)).scalar_one()
    assert stored.id == job.id


def test_a_real_job_index_violation_at_commit_surfaces(db, pull, monkeypatch):
    """The only test that makes db.commit() itself raise.

    Everything else here provokes the review index at db.flush(). That leaves
    the statement this handler was widened to cover, the commit, never being
    the thing that fails, and SQLAlchemy's session state after a failed commit
    is not the same as after a failed flush: the transaction is deactivated,
    so anything before the rollback raises PendingRollbackError.

    Reached without stubbing the classifier: a fresh head sha keeps the review
    index quiet, and pointing the job at a review that already owns a live one
    makes Postgres refuse at commit. The constraint name is then whatever the
    database actually said, so this also pins LIVE_JOB_INDEX's value rather
    than comparing it with itself.
    """
    user, pull_request = pull
    winner, _job = review_service.insert_review(
        db, user, pull_request, pull_request.head_sha, BASE_SHA
    )
    winner_id = winner.id
    real_job = review_service.ReviewJob
    monkeypatch.setattr(
        review_service, "ReviewJob", lambda review_id: real_job(review_id=winner_id)
    )

    with structlog.testing.capture_logs() as logs:
        with pytest.raises(review_service.IntegrityError):
            review_service.insert_review(db, user, pull_request, "d" * 40, BASE_SHA)

    errored = [e for e in logs if e["event"] == "review_insert_failed_unexpectedly"]
    assert errored, "a commit-time integrity error surfaced with nothing in the log"
    assert errored[0]["constraint"] == "uq_jobs_one_live_per_review"
