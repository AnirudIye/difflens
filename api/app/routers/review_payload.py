"""The JSON shape of a review, shared by the authenticated and demo routes.

Both routers answer with the same review object, so the shape lives here
rather than in either of them. A second copy would drift: a field added for
signed-in users and forgotten for the demo would leave the same page
rendering two different objects depending on how it was reached.
"""

from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Feedback, Finding, PullRequest, Repository, Review, User


def finding_item(finding: Finding, verdict: str | None) -> dict[str, Any]:
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


def pipeline_marker(review: Review, prefix: str) -> str | None:
    """One space-separated marker out of the recorded pipeline_version."""
    for token in (review.pipeline_version or "").split():
        if token.startswith(prefix):
            return token[len(prefix) :]
    return None


def ai_model(review: Review) -> str | None:
    """Which model produced the AI findings.

    None means no AI ran at all; "mock" means the offline stub ran, which is
    not a real review; "demo" means the public demo replayed its recorded
    response. The UI has to be able to tell all three from a real pass.
    """
    return pipeline_marker(review, "ai=")


def ai_skipped(review: Review) -> str | None:
    """Why the AI stage did not run, when it was the pipeline's decision.

    Without this, "the diff was too large" and "nobody configured a reviewer"
    both arrive as a null model, and the page offers to take an API key from
    a user who already has one and would not have been helped by it.
    """
    return pipeline_marker(review, "ai_skipped=")


def pull_context(pull: PullRequest, repository: Repository) -> dict[str, Any]:
    return {
        "id": str(pull.id),
        "number": pull.github_number,
        "title": pull.title,
        "html_url": pull.html_url,
        "repository_id": str(repository.id),
        "repository_full_name": repository.full_name,
    }


def review_item(
    review: Review,
    cancel_requested: bool,
    findings: list[Finding],
    verdicts: dict[UUID, str],
    context: dict[str, Any],
) -> dict[str, Any]:
    return {
        "id": str(review.id),
        "pull_request_id": str(review.pull_request_id),
        "pull_request": context,
        "ai_model": ai_model(review),
        "ai_skipped": ai_skipped(review),
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
        "findings": [finding_item(finding, verdicts.get(finding.id)) for finding in findings],
    }


def review_findings(db: Session, review: Review) -> list[Finding]:
    return list(
        db.execute(
            select(Finding)
            .where(Finding.review_id == review.id)
            .order_by(Finding.file_path, Finding.start_line, Finding.id)
        ).scalars()
    )


def feedback_verdicts(db: Session, user: User, findings: list[Finding]) -> dict[UUID, str]:
    if not findings:
        return {}
    rows = db.execute(
        select(Feedback.finding_id, Feedback.verdict).where(
            Feedback.user_id == user.id,
            Feedback.finding_id.in_([finding.id for finding in findings]),
        )
    )
    return {finding_id: verdict for finding_id, verdict in rows}


def load_pull_context(db: Session, review: Review) -> dict[str, Any]:
    # The review's FK cascades from pull_requests, so the row must exist
    pull, repository = db.execute(
        select(PullRequest, Repository)
        .join(Repository, Repository.id == PullRequest.repository_id)
        .where(PullRequest.id == review.pull_request_id)
    ).one()
    return pull_context(pull, repository)
