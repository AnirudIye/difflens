from typing import Any
from uuid import UUID

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import queue
from app.deps import CurrentUser, DbSession, GitHubDep
from app.models import Feedback, Finding, PullRequest, Repository, Review, User, UserRepository
from app.routers.github_errors import github_failure, not_found
from app.services import review_service
from app.services.github_client import GitHubError

router = APIRouter(prefix="/reviews")

MISSING = "Review not found"


class CreateReviewRequest(BaseModel):
    pull_request_id: UUID


def _finding_item(finding: Finding, verdict: str | None) -> dict[str, Any]:
    return {
        "id": str(finding.id),
        "file_path": finding.file_path,
        "start_line": finding.start_line,
        "end_line": finding.end_line,
        "severity": finding.severity,
        "category": finding.category,
        "confidence": finding.confidence,
        "source": finding.source,
        "status": finding.status,
        "title": finding.title,
        "explanation": finding.explanation,
        "recommendation": finding.recommendation,
        "feedback": verdict,
    }


def _ai_model(review: Review) -> str | None:
    """Which model produced the AI findings, from the recorded pipeline_version.

    None means no AI ran at all; "mock" means the offline stub ran, which is
    not a real review. The UI has to be able to tell those from a real pass.
    """
    for token in (review.pipeline_version or "").split():
        if token.startswith("ai="):
            return token[3:]
    return None


def _pull_context(pull: PullRequest, repository: Repository) -> dict[str, Any]:
    return {
        "id": str(pull.id),
        "number": pull.github_number,
        "title": pull.title,
        "html_url": pull.html_url,
        "repository_id": str(repository.id),
        "repository_full_name": repository.full_name,
    }


def _review_item(
    review: Review,
    cancel_requested: bool,
    findings: list[Finding],
    verdicts: dict[UUID, str],
    pull_context: dict[str, Any],
) -> dict[str, Any]:
    return {
        "id": str(review.id),
        "pull_request_id": str(review.pull_request_id),
        "pull_request": pull_context,
        "ai_model": _ai_model(review),
        "status": review.status,
        "head_sha": review.head_sha,
        "base_sha": review.base_sha,
        "summary": review.summary,
        "findings_count": review.findings_count,
        "severity_counts": review.severity_counts,
        "error_user_message": review.error_user_message,
        "created_at": review.created_at,
        "started_at": review.started_at,
        "completed_at": review.completed_at,
        "cancel_requested": cancel_requested,
        "findings": [_finding_item(finding, verdicts.get(finding.id)) for finding in findings],
    }


def _load_owned_review(db: Session, user: User, review_id: UUID) -> Review:
    review = db.execute(
        select(Review).where(Review.id == review_id, Review.user_id == user.id)
    ).scalar_one_or_none()
    if review is None:
        # A foreign review must look exactly like one that never existed
        raise not_found(MISSING)
    return review


def _review_findings(db: Session, review: Review) -> list[Finding]:
    return list(
        db.execute(
            select(Finding)
            .where(Finding.review_id == review.id)
            .order_by(Finding.file_path, Finding.start_line, Finding.id)
        ).scalars()
    )


def _cancel_requested(db: Session, review: Review) -> bool:
    job = review_service.latest_job(db, review)
    return job.cancel_requested if job else False


def _feedback_verdicts(db: Session, user: User, findings: list[Finding]) -> dict[UUID, str]:
    if not findings:
        return {}
    rows = db.execute(
        select(Feedback.finding_id, Feedback.verdict).where(
            Feedback.user_id == user.id,
            Feedback.finding_id.in_([finding.id for finding in findings]),
        )
    )
    return {finding_id: verdict for finding_id, verdict in rows}


def _load_pull_context(db: Session, review: Review) -> dict[str, Any]:
    # The review's FK cascades from pull_requests, so the row must exist
    pull, repository = db.execute(
        select(PullRequest, Repository)
        .join(Repository, Repository.id == PullRequest.repository_id)
        .where(PullRequest.id == review.pull_request_id)
    ).one()
    return _pull_context(pull, repository)


@router.post("", status_code=201)
def create_review(
    body: CreateReviewRequest, user: CurrentUser, db: DbSession, client: GitHubDep
) -> dict[str, Any]:
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
    return _review_item(
        review,
        cancel_requested=False,
        findings=[],
        verdicts={},
        pull_context=_pull_context(pull, repository),
    )


@router.get("/{review_id}")
def get_review(review_id: UUID, user: CurrentUser, db: DbSession) -> dict[str, Any]:
    review = _load_owned_review(db, user, review_id)
    findings = _review_findings(db, review)
    return _review_item(
        review,
        _cancel_requested(db, review),
        findings,
        _feedback_verdicts(db, user, findings),
        _load_pull_context(db, review),
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
    findings = _review_findings(db, review)
    return _review_item(
        review,
        _cancel_requested(db, review),
        findings,
        _feedback_verdicts(db, user, findings),
        _load_pull_context(db, review),
    )
