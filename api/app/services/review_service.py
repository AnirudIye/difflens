"""Create and cancel reviews. The worker owns every other transition.

Creation pins the snapshot: the PR is refetched from GitHub so the review
describes the branch as it is now, not as it was at the last sync. The
partial unique indexes (one live review per snapshot, one live job per
review) are the concurrency control; this module only translates their
violations into domain errors.
"""

import uuid
from datetime import UTC, datetime

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import PullRequest, Repository, Review, ReviewJob, User
from app.services.github_client import GitHubClient
from app.services.repo_service import apply_pull_payload

LIVE_REVIEW_STATUSES = ("queued", "running", "completed")
TERMINAL_REVIEW_STATUSES = ("completed", "failed", "cancelled")


class PullRequestClosed(Exception):
    pass


class ReviewAlreadyExists(Exception):
    """review_id is only set when the winning review belongs to the caller;
    a foreign user's review id is not theirs to see."""

    def __init__(self, review_id: uuid.UUID | None) -> None:
        super().__init__("a live review already covers this snapshot")
        self.review_id = review_id


class ReviewFinished(Exception):
    pass


class ReviewStillRunning(Exception):
    """Re-running a live review would race the worker that owns it."""


def create_review(
    db: Session,
    user: User,
    pull: PullRequest,
    repository: Repository,
    client: GitHubClient,
) -> tuple[Review, ReviewJob]:
    """Pin the PR's current snapshot and enqueue exactly one job for it.

    Raises PullRequestClosed, ReviewAlreadyExists, or any GitHubError from
    the refetch.
    """
    payload = client.get_pull(repository.full_name, pull.github_number)
    apply_pull_payload(pull, payload, datetime.now(UTC))
    if payload["state"] != "open":
        db.commit()  # keep the refreshed row even when we refuse the review
        raise PullRequestClosed()

    review = Review(
        user_id=user.id,
        pull_request_id=pull.id,
        head_sha=payload["head"]["sha"],
        base_sha=payload["base"]["sha"],
        status="queued",
    )
    db.add(review)
    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        existing = db.execute(
            select(Review).where(
                Review.pull_request_id == pull.id,
                Review.head_sha == payload["head"]["sha"],
                Review.status.in_(LIVE_REVIEW_STATUSES),
            )
        ).scalar_one_or_none()
        if existing is None:
            raise  # some other integrity problem; let it surface
        raise ReviewAlreadyExists(existing.id if existing.user_id == user.id else None) from exc

    job = ReviewJob(review_id=review.id)
    db.add(job)
    db.commit()
    return review, job


def rerun_review(
    db: Session,
    user: User,
    review: Review,
    pull: PullRequest,
    repository: Repository,
    client: GitHubClient,
) -> tuple[Review, ReviewJob]:
    """Review this pull request again, superseding the finished review.

    The point is re-reviewing the same commit after something about the
    reviewer changed (an AI key, usually). A finished review at that commit
    would otherwise block the new one through uq_reviews_pr_sha_live, so it
    moves to superseded first, which sits outside that index.

    Superseding is a guarded UPDATE, so of two concurrent re-runs only one
    wins the row and the loser's create attempt hits the index and surfaces
    as ReviewAlreadyExists rather than quietly duplicating work.
    """
    if review.status not in TERMINAL_REVIEW_STATUSES:
        raise ReviewStillRunning()

    superseded = db.execute(
        update(Review)
        .where(Review.id == review.id, Review.status == review.status)
        .values(status="superseded")
        .returning(Review.id)
    ).first()
    if superseded is None:
        # Someone else moved this row first; let the index arbitrate below
        db.rollback()
    return create_review(db, user, pull, repository, client)


def latest_job(db: Session, review: Review) -> ReviewJob | None:
    return db.execute(
        select(ReviewJob)
        .where(ReviewJob.review_id == review.id)
        .order_by(ReviewJob.created_at.desc())
        .limit(1)
    ).scalar_one_or_none()


def cancel_review(db: Session, review: Review) -> Review:
    """Cancel a queued review outright; ask a running one to stop.

    One flag-raising UPDATE covers both live states, so a job bouncing
    between queued and running (worker retries) can never slip through the
    gap between two separately-guarded statements: whichever state it lands
    in, cancel_requested is set and either this function, the worker's
    checkpoint, or the sweep finishes the cancellation.
    """
    if review.status in TERMINAL_REVIEW_STATUSES:
        raise ReviewFinished()

    now = datetime.now(UTC)
    flagged = db.execute(
        update(ReviewJob)
        .where(ReviewJob.review_id == review.id, ReviewJob.status.in_(("queued", "running")))
        .values(cancel_requested=True)
        .returning(ReviewJob.id, ReviewJob.status)
    ).first()
    if flagged is not None and flagged.status == "queued":
        finalized = db.execute(
            update(ReviewJob)
            .where(ReviewJob.id == flagged.id, ReviewJob.status == "queued")
            .values(status="cancelled", finished_at=now)
            .returning(ReviewJob.id)
        ).first()
        # If a worker claimed it between the two statements, the flag is
        # already up and the worker's checkpoint completes the cancel
        if finalized is not None:
            review.status = "cancelled"
            review.completed_at = now
    db.commit()
    return review
