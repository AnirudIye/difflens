from typing import Any
from uuid import UUID

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, model_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import queue
from app.deps import CurrentUser, DbSession, GitHubDep
from app.models import PullRequest, Repository, Review, User, UserRepository
from app.rate_limit import ReviewRateLimit
from app.routers import review_payload
from app.routers.github_errors import github_failure, not_found
from app.services import review_service
from app.services.github_client import GitHubError

router = APIRouter(prefix="/reviews")

MISSING = "Review not found"


class CreateReviewRequest(BaseModel):
    pull_request_id: UUID | None = None
    repository_id: UUID | None = None

    @model_validator(mode="after")
    def _exactly_one_target(self) -> "CreateReviewRequest":
        if (self.pull_request_id is None) == (self.repository_id is None):
            raise ValueError("Provide exactly one of pull_request_id or repository_id")
        return self


def _load_owned_review(db: Session, user: User, review_id: UUID) -> Review:
    review = db.execute(
        select(Review).where(Review.id == review_id, Review.user_id == user.id)
    ).scalar_one_or_none()
    if review is None:
        # A foreign review must look exactly like one that never existed
        raise not_found(MISSING)
    return review


def _cancel_requested(db: Session, review: Review) -> bool:
    job = review_service.latest_job(db, review)
    return job.cancel_requested if job else False


def _load_owned_repository(db: Session, user: User, repository_id: UUID) -> Repository | None:
    """The repository, if this user is linked to it and it is not the demo.

    The demo repository is excluded structurally: it belongs to no user, but
    is_demo is checked anyway so a future seeding change cannot quietly make
    the operator's demo reviewable through the authenticated path.
    """
    return db.execute(
        select(Repository)
        .join(UserRepository, UserRepository.repository_id == Repository.id)
        .where(
            UserRepository.user_id == user.id,
            Repository.id == repository_id,
            Repository.is_demo.is_(False),
        )
    ).scalar_one_or_none()


def _repo_conflicts(exc: Exception) -> HTTPException:
    if isinstance(exc, review_service.RepositoryEmpty):
        return HTTPException(
            status_code=409,
            detail={
                "code": "repository_empty",
                "message": (
                    "DiffLens could not find a commit to review: the repository may "
                    "be empty or its default branch may have been renamed. Refresh "
                    "from GitHub and try again."
                ),
            },
        )
    assert isinstance(exc, review_service.ReviewAlreadyExists)
    detail: dict[str, str] = {
        "code": "review_already_exists",
        "message": "A live review already covers this repository at this commit",
    }
    if exc.review_id is not None:  # only the caller's own review id is theirs to see
        detail["review_id"] = str(exc.review_id)
    return HTTPException(status_code=409, detail=detail)


@router.post("", status_code=201)
def create_review(
    body: CreateReviewRequest,
    user: CurrentUser,
    _limit: ReviewRateLimit,
    db: DbSession,
    client: GitHubDep,
) -> dict[str, Any]:
    if body.repository_id is not None:
        repository = _load_owned_repository(db, user, body.repository_id)
        if repository is None:
            raise not_found("Repository not found")
        try:
            review, job = review_service.create_repo_review(db, user, repository, client)
        except GitHubError as exc:
            raise github_failure(db, user, exc, "Repository not found") from exc
        except (review_service.RepositoryEmpty, review_service.ReviewAlreadyExists) as exc:
            raise _repo_conflicts(exc) from exc
        queue.notify(queue.get_redis(), job.id)
        return review_payload.review_item(
            review,
            cancel_requested=False,
            findings=[],
            verdicts={},
            context=review_payload.repo_review_context(repository),
        )

    row = db.execute(
        select(PullRequest, Repository)
        .join(Repository, Repository.id == PullRequest.repository_id)
        .join(UserRepository, UserRepository.repository_id == Repository.id)
        .where(UserRepository.user_id == user.id, PullRequest.id == body.pull_request_id)
    ).first()
    if row is None:
        raise not_found("Pull request not found")
    pull, repository = row

    try:
        review, job = review_service.create_review(db, user, pull, repository, client)
    except GitHubError as exc:
        raise github_failure(db, user, exc, "Pull request not found") from exc
    except review_service.PullRequestClosed:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "pull_request_closed",
                "message": "This pull request is no longer open on GitHub",
            },
        ) from None
    except review_service.ReviewAlreadyExists as exc:
        detail: dict[str, str] = {
            "code": "review_already_exists",
            "message": "A live review already covers this pull request at this commit",
        }
        if exc.review_id is not None:  # only the caller's own review id is theirs to see
            detail["review_id"] = str(exc.review_id)
        raise HTTPException(status_code=409, detail=detail) from exc

    # After commit so a lost doorbell can only delay the job, never orphan it;
    # the worker's sweep re-rings for anything queued and unheard
    queue.notify(queue.get_redis(), job.id)
    return review_payload.review_item(
        review,
        cancel_requested=False,
        findings=[],
        verdicts={},
        context=review_payload.pull_review_context(pull, repository),
    )


@router.post("/{review_id}/rerun", status_code=201)
def rerun_review(
    review_id: UUID,
    user: CurrentUser,
    _limit: ReviewRateLimit,
    db: DbSession,
    client: GitHubDep,
) -> dict[str, Any]:
    """Review the same target again, superseding this finished review."""
    review = _load_owned_review(db, user, review_id)

    if review.repository_id is not None:
        repository = _load_owned_repository(db, user, review.repository_id)
        if repository is None:
            raise not_found("Repository not found")
        try:
            fresh, job = review_service.rerun_repo_review(db, user, review, repository, client)
        except review_service.ReviewStillRunning:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "review_still_running",
                    "message": "This review has not finished yet",
                },
            ) from None
        except GitHubError as exc:
            raise github_failure(db, user, exc, "Repository not found") from exc
        except (review_service.RepositoryEmpty, review_service.ReviewAlreadyExists) as exc:
            raise _repo_conflicts(exc) from exc
        queue.notify(queue.get_redis(), job.id)
        return review_payload.review_item(
            fresh,
            cancel_requested=False,
            findings=[],
            verdicts={},
            context=review_payload.repo_review_context(repository),
        )

    row = db.execute(
        select(PullRequest, Repository)
        .join(Repository, Repository.id == PullRequest.repository_id)
        .join(UserRepository, UserRepository.repository_id == Repository.id)
        .where(UserRepository.user_id == user.id, PullRequest.id == review.pull_request_id)
    ).first()
    if row is None:
        raise not_found("Pull request not found")
    pull, repository = row

    try:
        fresh, job = review_service.rerun_review(db, user, review, pull, repository, client)
    except review_service.ReviewStillRunning:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "review_still_running",
                "message": "This review has not finished yet",
            },
        ) from None
    except GitHubError as exc:
        raise github_failure(db, user, exc, "Pull request not found") from exc
    except review_service.PullRequestClosed:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "pull_request_closed",
                "message": "This pull request is no longer open on GitHub",
            },
        ) from None
    except review_service.ReviewAlreadyExists as exc:
        detail: dict[str, str] = {
            "code": "review_already_exists",
            "message": "A live review already covers this pull request at this commit",
        }
        if exc.review_id is not None:
            detail["review_id"] = str(exc.review_id)
        raise HTTPException(status_code=409, detail=detail) from exc

    queue.notify(queue.get_redis(), job.id)
    return review_payload.review_item(
        fresh,
        cancel_requested=False,
        findings=[],
        verdicts={},
        context=review_payload.pull_review_context(pull, repository),
    )


@router.get("/{review_id}")
def get_review(review_id: UUID, user: CurrentUser, db: DbSession) -> dict[str, Any]:
    review = _load_owned_review(db, user, review_id)
    findings = review_payload.review_findings(db, review)
    return review_payload.review_item(
        review,
        _cancel_requested(db, review),
        findings,
        review_payload.feedback_verdicts(db, user, findings),
        review_payload.load_review_context(db, review),
    )


@router.post("/{review_id}/cancel")
def cancel_review(review_id: UUID, user: CurrentUser, db: DbSession) -> dict[str, Any]:
    review = _load_owned_review(db, user, review_id)
    try:
        review = review_service.cancel_review(db, review)
    except review_service.ReviewFinished:
        raise HTTPException(
            status_code=409,
            detail={"code": "review_finished", "message": "This review has already finished"},
        ) from None
    findings = review_payload.review_findings(db, review)
    return review_payload.review_item(
        review,
        _cancel_requested(db, review),
        findings,
        review_payload.feedback_verdicts(db, user, findings),
        review_payload.load_review_context(db, review),
    )
