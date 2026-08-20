"""Seeding and lookups for the one demo pull request.

Every query here is anchored on `Repository.is_demo`. That is what makes the
public endpoints safe to expose without a session: there is no parameter a
caller can supply that widens the scope, because the scope is a column value
and not an argument.
"""

import structlog
from sqlalchemy import case, delete, select
from sqlalchemy.orm import Session

from app.demo import sample
from app.models import PullRequest, Repository, Review, ReviewJob, User
from app.services import review_service

log = structlog.get_logger()

# Real GitHub ids start at 1, so 0 can never collide with a real account or
# a real repository. The demo user deliberately has NO ProviderConnection:
# it holds no token, so the demo path cannot reach GitHub even by mistake.
DEMO_GITHUB_ID = 0
DEMO_LOGIN = "difflens-demo"
DEMO_NAME = "DiffLens demo"

# How many finished demo reviews to keep. Anonymous visitors can rerun, so
# something has to bound the row count; the live-review index already bounds
# the rate at one at a time.
HISTORY_KEEP = 3


class DemoNotSeeded(Exception):
    """The demo is switched on but its rows are missing."""


class DemoReviewRunning(Exception):
    """A demo review is already queued or running; let it finish."""


def demo_repository(db: Session) -> Repository | None:
    return db.execute(select(Repository).where(Repository.is_demo)).scalar_one_or_none()


def demo_context(db: Session) -> tuple[User, Repository, PullRequest]:
    """The seeded user, repository, and pull request, or DemoNotSeeded."""
    repository = demo_repository(db)
    if repository is None:
        raise DemoNotSeeded("no repository is marked is_demo")
    pull = db.execute(
        select(PullRequest).where(PullRequest.repository_id == repository.id)
    ).scalar_one_or_none()
    if pull is None:
        raise DemoNotSeeded("the demo repository has no pull request")
    user = db.execute(select(User).where(User.github_id == DEMO_GITHUB_ID)).scalar_one_or_none()
    if user is None:
        raise DemoNotSeeded("the demo user is missing")
    return user, repository, pull


# What "current" means, in order. Status decides before recency because
# created_at defaults to Postgres now(), which is the TRANSACTION start time
# and therefore identical for two rows written in one transaction. Ordering
# on the timestamp alone left the tiebreak to a random UUID, so straight
# after a rerun the page could show the superseded review instead of the one
# the visitor had just started.
_CURRENT_REVIEW_RANK = case(
    (Review.status.in_(("queued", "running")), 0),
    (Review.status == "completed", 1),
    (Review.status.in_(("failed", "cancelled")), 2),
    else_=3,  # superseded: never current, it has been replaced by definition
)


def current_review(db: Session) -> Review | None:
    """The demo review a visitor should be looking at.

    A live review outranks a finished one, so pressing rerun shows the run
    that was just started rather than the one it replaced.
    """
    repository = demo_repository(db)
    if repository is None:
        return None
    return db.execute(
        select(Review)
        .join(PullRequest, PullRequest.id == Review.pull_request_id)
        .where(PullRequest.repository_id == repository.id)
        .order_by(_CURRENT_REVIEW_RANK, Review.created_at.desc(), Review.id.desc())
        .limit(1)
    ).scalar_one_or_none()


def seed(db: Session) -> tuple[Review, ReviewJob] | None:
    """Create the demo rows if they are missing. Safe to run on every boot.

    Returns the queued review and job when this call created them, so the
    caller can ring the doorbell, or None when the demo was already seeded.

    Nothing here is fetched: the pull request is described by constants in
    `sample.py` and its content by the files beside them. The first review is
    enqueued rather than inserted complete, so even the demo's opening state
    is something the real pipeline produced.
    """
    user = db.execute(select(User).where(User.github_id == DEMO_GITHUB_ID)).scalar_one_or_none()
    if user is None:
        user = User(github_id=DEMO_GITHUB_ID, login=DEMO_LOGIN, name=DEMO_NAME)
        db.add(user)
        db.flush()

    repository = demo_repository(db)
    if repository is None:
        repository = Repository(
            github_id=DEMO_GITHUB_ID,
            owner=sample.REPO_FULL_NAME.split("/")[0],
            name=sample.REPO_FULL_NAME.split("/")[1],
            full_name=sample.REPO_FULL_NAME,
            private=False,
            default_branch="main",
            html_url=sample.REPO_HTML_URL,
            is_demo=True,
        )
        db.add(repository)
        db.flush()

    pull = db.execute(
        select(PullRequest).where(PullRequest.repository_id == repository.id)
    ).scalar_one_or_none()
    if pull is None:
        pull = PullRequest(
            repository_id=repository.id,
            github_number=sample.PR_NUMBER,
            title=sample.PR_TITLE,
            author_login=sample.PR_AUTHOR,
            state="open",
            base_ref="main",
            head_ref="settlement-reporting",
            base_sha=sample.BASE_SHA,
            head_sha=sample.HEAD_SHA,
            html_url=sample.PR_HTML_URL or None,
        )
        db.add(pull)
        db.flush()

    db.commit()

    if current_review(db) is not None:
        return None
    log.info("demo_seed_enqueueing_first_review")
    return review_service.insert_review(db, user, pull, sample.HEAD_SHA, sample.BASE_SHA)


def rerun(db: Session) -> tuple[Review, ReviewJob]:
    """Review the demo pull request again, replacing the finished review.

    Refuses while one is live rather than racing the worker that owns it.
    The live-review index would refuse anyway; this turns that into an
    honest answer instead of an integrity error.
    """
    user, _repository, pull = demo_context(db)
    existing = current_review(db)
    if existing is not None:
        if existing.status in ("queued", "running"):
            raise DemoReviewRunning()
        review_service.supersede_completed(db, existing)
    fresh = review_service.insert_review(db, user, pull, sample.HEAD_SHA, sample.BASE_SHA)
    prune(db)
    return fresh


def prune(db: Session, keep: int = HISTORY_KEEP) -> int:
    """Drop finished demo reviews beyond the newest `keep`, findings and all.

    `keep` counts finished reviews only. A queued or running review is
    excluded from the candidate set outright rather than competing for a
    slot, so a rerun started at the moment of a prune cannot be deleted out
    from under the worker that owns it.
    """
    repository = demo_repository(db)
    if repository is None:
        return 0
    stale = (
        db.execute(
            select(Review.id)
            .join(PullRequest, PullRequest.id == Review.pull_request_id)
            .where(PullRequest.repository_id == repository.id)
            .where(Review.status.not_in(("queued", "running")))
            .order_by(Review.created_at.desc(), Review.id.desc())
            .offset(keep)
        )
        .scalars()
        .all()
    )
    if not stale:
        return 0
    db.execute(delete(Review).where(Review.id.in_(stale)))
    db.commit()
    log.info("demo_reviews_pruned", removed=len(stale))
    return len(stale)
