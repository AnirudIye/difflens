"""Review creation at the seam the API cannot reach.

Everything else about creating a review is exercised through the routers in
test_reviews_api.py. These tests call the service directly, because the
behaviour under test lives in a window inside one function and a request has
no way to land in the middle of it.
"""

import pytest
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


def test_a_job_index_violation_is_a_conflict_not_a_server_error(db, pull, monkeypatch):
    """The job insert is inside the handler too, not just the review insert.

    uq_jobs_one_live_per_review is unreachable through insert_review today,
    because the review id is fresh every time, but it is the same shape of
    integrity error one line further down and used to escape raw as a 500.
    Simulated by reporting the job index as the violated constraint.
    """
    user, pull_request = pull
    winner, _job = review_service.insert_review(
        db, user, pull_request, pull_request.head_sha, BASE_SHA
    )
    _supersede_during_recovery(db, monkeypatch, winner.id)
    monkeypatch.setattr(
        review_service, "_violated_constraint", lambda exc: review_service.LIVE_JOB_INDEX
    )

    with pytest.raises(review_service.ReviewAlreadyExists) as excinfo:
        review_service.insert_review(db, user, pull_request, pull_request.head_sha, BASE_SHA)

    assert excinfo.value.review_id is None


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
