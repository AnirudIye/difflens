"""Create and cancel reviews. The worker owns every other transition.

Creation pins the snapshot: the PR is refetched from GitHub so the review
describes the branch as it is now, not as it was at the last sync. The
partial unique indexes (one live review per snapshot, one live job per
review) are the concurrency control; this module only translates their
violations into domain errors.
"""

import uuid
from datetime import UTC, datetime

import structlog
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import (
    LIVE_REPO_REVIEW_INDEX,
    LIVE_REVIEW_INDEX,
    PullRequest,
    Repository,
    Review,
    ReviewJob,
    User,
)
from app.services.github_client import GitHubClient, GitHubNotFound
from app.services.repo_service import apply_pull_payload

log = structlog.get_logger()

LIVE_REVIEW_STATUSES = ("queued", "running", "completed")
TERMINAL_REVIEW_STATUSES = ("completed", "failed", "cancelled")


class PullRequestClosed(Exception):
    pass


class RepositoryEmpty(Exception):
    """No commit to review: the repository is empty or its default branch
    is gone (renamed, deleted). Either way there is nothing to pin."""


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
    payload = refetch_open_pull(db, pull, repository, client)
    return _insert_review(db, user, pull, payload)


def refetch_open_pull(
    db: Session, pull: PullRequest, repository: Repository, client: GitHubClient
) -> dict:
    """Refresh the stored pull request from GitHub, and refuse a closed one.

    Split out so a rerun can ask this question BEFORE it touches the review
    it is replacing. The commit below is exactly why that matters: it
    persists the refreshed row, and it would just as happily persist a
    half-finished rerun that is about to be refused.
    """
    payload = client.get_pull(repository.full_name, pull.github_number)
    apply_pull_payload(pull, payload, datetime.now(UTC))
    if payload["state"] != "open":
        db.commit()  # keep the refreshed row even when we refuse the review
        raise PullRequestClosed()
    return payload


def _violated_constraint(exc: IntegrityError) -> str | None:
    """The constraint Postgres rejected the write on, when it named one.

    psycopg carries it on the diagnostics of the driver error. Anything that
    is not a psycopg error, and any driver that declines to say, gives None,
    which callers must treat as "unknown" rather than "not that one".
    """
    diagnostics = getattr(getattr(exc, "orig", None), "diag", None)
    return getattr(diagnostics, "constraint_name", None)


def _insert_queued(
    db: Session,
    user: User,
    review: Review,
    live_index: str,
    winner_where: tuple,
    log_target: dict[str, str],
) -> tuple[Review, ReviewJob]:
    """Insert one queued review plus its job; translate the live-index race.

    Shared by the pull request and repository paths, which differ only in
    which partial unique index arbitrates and how the winning review is
    found. Behavior on the PR path is byte-identical to before the split.
    """
    db.add(review)
    try:
        db.flush()
        # After the flush, never before: reviews.id comes from the database
        # (server_default gen_random_uuid), so it is None until then
        job = ReviewJob(review_id=review.id)
        db.add(job)
        db.commit()
    except IntegrityError as exc:
        # Read before the rollback, so which index refused the write is
        # settled by the database rather than inferred from a second query
        conflicted_on = _violated_constraint(exc)
        db.rollback()
        if conflicted_on != live_index:
            # Only this index means "someone else got there first". The commit
            # above flushes the WHOLE session, not just these two rows, so a
            # refetched pull request or a superseded review can be what
            # Postgres actually refused; classifying on the presence of a live
            # review rather than on the constraint would dress any of those as
            # a 409. So would uq_jobs_one_live_per_review, which cannot lose a
            # race at all, because reviews.id is a fresh gen_random_uuid on
            # every call and no earlier job can reference it. Telling the
            # caller to wait for a review that does not exist, with nothing in
            # the log, is worse than the 500 this used to be.
            log.error(
                "review_insert_failed_unexpectedly",
                constraint=conflicted_on,
                head_sha=review.head_sha,
                user_id=str(user.id),
                **log_target,
            )
            raise
        existing = db.execute(
            select(Review).where(
                *winner_where,
                Review.head_sha == review.head_sha,
                Review.status.in_(LIVE_REVIEW_STATUSES),
            )
        ).scalar_one_or_none()
        # The winner can leave the live statuses between the failed INSERT and
        # that read, which is why this is logged either way: a conflict with
        # nothing left to name is the interesting one.
        log.warning(
            "review_insert_conflict",
            head_sha=review.head_sha,
            winner_still_live=existing is not None,
            **log_target,
        )
        if existing is not None:
            raise ReviewAlreadyExists(existing.id if existing.user_id == user.id else None) from exc
        raise ReviewAlreadyExists(None) from exc

    return review, job


def insert_review(
    db: Session, user: User, pull: PullRequest, head_sha: str, base_sha: str
) -> tuple[Review, ReviewJob]:
    """Insert one queued review at a pinned snapshot, plus its job.

    Takes the two SHAs rather than a GitHub payload so the public demo, which
    has no GitHub payload and never talks to GitHub, enqueues through exactly
    this path: the same live-review index arbitration, the same one-job-per
    -review guarantee. A second code path here is the kind of thing that
    drifts and then only breaks for the demo.
    """
    review = Review(
        user_id=user.id,
        pull_request_id=pull.id,
        head_sha=head_sha,
        base_sha=base_sha,
        status="queued",
    )
    return _insert_queued(
        db,
        user,
        review,
        LIVE_REVIEW_INDEX,
        (Review.pull_request_id == pull.id,),
        {"pull_request_id": str(pull.id)},
    )


def insert_repo_review(
    db: Session, user: User, repository: Repository, head_sha: str
) -> tuple[Review, ReviewJob]:
    """Insert one queued repository snapshot review at a pinned commit."""
    review = Review(
        user_id=user.id,
        repository_id=repository.id,
        head_sha=head_sha,
        base_sha=None,
        status="queued",
    )
    return _insert_queued(
        db,
        user,
        review,
        LIVE_REPO_REVIEW_INDEX,
        (Review.repository_id == repository.id,),
        {"repository_id": str(repository.id)},
    )


def create_repo_review(
    db: Session, user: User, repository: Repository, client: GitHubClient
) -> tuple[Review, ReviewJob]:
    """Pin the default branch's current head and enqueue exactly one job.

    Mirrors create_review's shape: refresh from GitHub first so the review
    describes the repository as it is now, then let the live index arbitrate.
    Raises RepositoryEmpty, ReviewAlreadyExists, or any GitHubError.
    """
    head_sha = _resolve_default_branch_head(db, repository, client)
    return insert_repo_review(db, user, repository, head_sha)


def _resolve_default_branch_head(db: Session, repository: Repository, client: GitHubClient) -> str:
    payload = client.get_repo(repository.full_name)
    branch = payload.get("default_branch") or repository.default_branch
    if branch and branch != repository.default_branch:
        repository.default_branch = branch
        db.commit()  # keep the refreshed row even if the head lookup fails
    if not branch:
        raise RepositoryEmpty()
    try:
        return client.get_branch_head(repository.full_name, branch)
    except GitHubNotFound as exc:
        raise RepositoryEmpty() from exc


def _insert_review(
    db: Session, user: User, pull: PullRequest, payload: dict
) -> tuple[Review, ReviewJob]:
    return insert_review(db, user, pull, payload["head"]["sha"], payload["base"]["sha"])


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
    reviewer changed (an AI key, usually). A completed review at that commit
    would otherwise block the new one through uq_reviews_pr_sha_live, so it
    moves to superseded first, which sits outside that index.

    Superseding is a guarded UPDATE, so of two concurrent re-runs only one
    wins the row and the loser's create attempt hits the index and surfaces
    as ReviewAlreadyExists rather than quietly duplicating work.

    Only a completed review is superseded. Failed and cancelled reviews are
    outside the index already, so overwriting their status would buy nothing
    and would destroy the record of how they ended: the page would render a
    failed review as a clean pass and its error message would vanish.
    """
    if review.status not in TERMINAL_REVIEW_STATUSES:
        raise ReviewStillRunning()

    # Before the supersede, never after. create_review commits on the closed
    # path, which used to commit the pending supersede along with it and
    # leave the old review replaced by a review that was never created.
    payload = refetch_open_pull(db, pull, repository, client)

    supersede_completed(db, review)
    return _insert_review(db, user, pull, payload)


def rerun_repo_review(
    db: Session,
    user: User,
    review: Review,
    repository: Repository,
    client: GitHubClient,
) -> tuple[Review, ReviewJob]:
    """Review this repository again, superseding the finished review.

    The head is re-resolved rather than reused: "run again" on a repository
    naturally means the default branch as it is now, so after new commits
    land the new review covers the new head. Same supersede-then-insert
    ordering and the same guarded UPDATE as the pull request rerun.
    """
    if review.status not in TERMINAL_REVIEW_STATUSES:
        raise ReviewStillRunning()

    # Before the supersede, for the same reason refetch_open_pull runs before
    # it on the PR path: a refused rerun must not leave the old review
    # superseded with nothing replacing it.
    head_sha = _resolve_default_branch_head(db, repository, client)

    supersede_completed(db, review)
    return insert_repo_review(db, user, repository, head_sha)


def supersede_completed(db: Session, review: Review) -> None:
    """Move a completed review out of the live index so a rerun can replace it.

    Guarded on the status it was read at, so of two concurrent reruns only one
    wins the row; the loser falls through to the index and surfaces as
    ReviewAlreadyExists rather than quietly duplicating work.

    Only a completed review is touched. Failed and cancelled reviews are
    outside the index already, so overwriting their status would buy nothing
    and would destroy the record of how they ended.
    """
    if review.status != "completed":
        return
    superseded = db.execute(
        update(Review)
        .where(Review.id == review.id, Review.status == review.status)
        .values(status="superseded")
        .returning(Review.id)
    ).first()
    if superseded is None:
        # Someone else moved this row first; let the index arbitrate
        db.rollback()


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
