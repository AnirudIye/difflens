"""Run one claimed review job end to end.

Fetches the pinned snapshot (compare on base_sha...head_sha, never branch
names), rebuilds the unified diff, materializes head-state files into a
throwaway workspace, runs the pure analysis pipeline, and persists findings
plus the terminal transition in one commit.

Failure taxonomy:
- transient (GitHub 5xx, rate limits, anything unexpected): requeue with
  exponential backoff until attempts run out
- permanent (snapshot gone, revoked token, unparseable diff): fail now and
  spend no retries
- cancellation: checked at every stage boundary; the worker owns the
  running -> cancelled transition
"""

import tempfile
import uuid
from collections import Counter
from pathlib import Path

import structlog
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.analysis.models import ReviewJob as AnalysisJob
from app.analysis.models import ReviewResult
from app.analysis.pipeline import AnalysisError, run_review
from app.models import (
    Finding,
    ProviderConnection,
    PullRequest,
    Repository,
    Review,
    ReviewJob,
)
from app.security import decrypt_token
from app.services.github_client import (
    GitHubAuthError,
    GitHubClient,
    GitHubNotFound,
    GitHubRateLimited,
)
from worker import jobs

log = structlog.get_logger()

RECONNECT_MESSAGE = "GitHub access is missing or was revoked; reconnect and run again"
SNAPSHOT_GONE_MESSAGE = "The reviewed commits are no longer reachable on GitHub"
BAD_DIFF_MESSAGE = "GitHub returned a diff this pipeline could not parse"
TRANSIENT_MESSAGE = "GitHub did not respond; the review will retry shortly"
EXHAUSTED_MESSAGE = "The review kept failing and was stopped; run it again to retry"
TOO_MANY_FILES_MESSAGE = (
    "This pull request changes too many files to review (GitHub caps the comparison at 300 files)"
)

# GitHub's compare endpoint hard-caps files[] at 300 with no way to page the
# file list; at the cap we cannot tell a complete diff from a truncated one
COMPARE_FILE_CAP = 300


def build_diff_text(files: list[dict]) -> str:
    """Reassemble one unified diff from GitHub's per-file compare patches.

    Files without a patch (binaries, >300-line generated blobs GitHub elides)
    contribute nothing: no patch means no changed lines to analyze.
    """
    parts: list[str] = []
    for item in files:
        patch = item.get("patch")
        if not patch:
            continue
        new = item["filename"]
        old = item.get("previous_filename") or new
        parts.append(f"diff --git a/{old} b/{new}")
        if item["status"] == "added":
            parts.append("new file mode 100644")
            parts.append("--- /dev/null")
            parts.append(f"+++ b/{new}")
        elif item["status"] == "removed":
            parts.append("deleted file mode 100644")
            parts.append(f"--- a/{old}")
            parts.append("+++ /dev/null")
        else:
            parts.append(f"--- a/{old}")
            parts.append(f"+++ b/{new}")
        parts.append(patch.rstrip("\n"))
    return "\n".join(parts) + "\n" if parts else ""


def populate_workspace(
    client: GitHubClient,
    full_name: str,
    head_sha: str,
    files: list[dict],
    workspace: Path,
) -> None:
    """Fetch head-state contents of changed files into the workspace.

    A file GitHub will not inline (>1MB) is skipped: the analyzers simply do
    not see it. A path that would escape the workspace is hostile input from
    the diff and is dropped before any request is made for it.
    """
    root = workspace.resolve()
    for item in files:
        if item["status"] == "removed" or not item.get("patch"):
            continue
        rel = item["filename"]
        destination = (root / rel).resolve()
        if not destination.is_relative_to(root):
            log.warning("workspace_escape_dropped", path=rel)
            continue
        blob = client.get_file_content(full_name, rel, head_sha)
        if blob is None:
            log.info("workspace_file_skipped", path=rel)
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(blob)


def _persist_success(
    db: Session, job: ReviewJob, review: Review, result: ReviewResult, worker_id: str
) -> bool:
    """Persist findings and the terminal transition, fenced on ownership.

    The job UPDATE is the gate: if the sweep reclaimed this job while we were
    analyzing, it matches zero rows and everything (findings included) rolls
    back, leaving the current owner's run untouched.
    """
    now = jobs._now()
    won = db.execute(
        update(ReviewJob)
        .where(
            ReviewJob.id == job.id,
            ReviewJob.status == "running",
            ReviewJob.locked_by == worker_id,
        )
        .values(status="completed", finished_at=now)
        .returning(ReviewJob.id)
    ).first()
    if won is None:
        db.rollback()
        return False

    severity_counts = dict(Counter(finding.severity for finding in result.findings))
    for finding in result.findings:
        db.add(
            Finding(
                review_id=review.id,
                file_path=finding.file_path,
                start_line=finding.start_line,
                end_line=finding.end_line,
                severity=finding.severity,
                category=finding.category,
                confidence=finding.confidence,
                source=finding.source,
                fingerprint=finding.fingerprint,
                title=finding.title,
                explanation=finding.explanation,
                recommendation=finding.recommendation,
            )
        )
    versions = result.stats.tool_versions
    pipeline_version = "deterministic_only " + " ".join(
        f"{tool}={version}" for tool, version in sorted(versions.items())
    )
    db.execute(
        update(Review)
        .where(Review.id == review.id)
        .values(
            status="completed",
            summary=result.summary,
            findings_count=len(result.findings),
            severity_counts=severity_counts,
            pipeline_version=pipeline_version,
            completed_at=now,
        )
    )
    db.commit()
    return True


def _checkpoint(db: Session, job: ReviewJob, worker_id: str) -> str | None:
    """Between stages: is this job still ours, and does anyone want it stopped?

    Returns "cancelled", "lost", or None (carry on). The ownership check is
    what lets a sweep-reclaimed worker notice and stand down instead of
    finishing a job that now belongs to someone else.
    """
    row = db.execute(
        select(ReviewJob.cancel_requested, ReviewJob.status, ReviewJob.locked_by).where(
            ReviewJob.id == job.id
        )
    ).first()
    if row is None or row.status != "running" or row.locked_by != worker_id:
        return "lost"
    if row.cancel_requested:
        return "cancelled" if jobs.cancel_job(db, job, worker_id) else "lost"
    return None


def process_job(db: Session, job_id: uuid.UUID, worker_id: str) -> str:
    """Claim and run one job. Returns the outcome for logging and tests:
    not_claimed | completed | retried | failed | cancelled | lost.
    """
    job = jobs.claim_job(db, job_id, worker_id)
    if job is None:
        return "not_claimed"
    return run_claimed_job(db, job, worker_id)


def run_claimed_job(db: Session, job: ReviewJob, worker_id: str) -> str:
    """Run a job this worker already claimed (the loop claims before it
    starts the heartbeat thread, so a beat can never race its own claim)."""
    review = db.get(Review, job.review_id)
    assert review is not None  # FK guarantees it; reviews are never hard-deleted
    log = structlog.get_logger().bind(job_id=str(job.id), review_id=str(review.id))

    try:
        if outcome := _checkpoint(db, job, worker_id):
            return outcome

        pull = db.get(PullRequest, review.pull_request_id)
        assert pull is not None
        repository = db.get(Repository, pull.repository_id)
        assert repository is not None
        connection = db.execute(
            select(ProviderConnection).where(
                ProviderConnection.user_id == review.user_id,
                ProviderConnection.provider == "github",
            )
        ).scalar_one_or_none()
        if connection is None or connection.token_invalid:
            return _fail_or_lost(
                db, job, worker_id, RECONNECT_MESSAGE, "github connection missing or invalid"
            )

        with GitHubClient(decrypt_token(connection.access_token_enc)) as client:
            compare = client.compare(repository.full_name, review.base_sha, review.head_sha)
            if outcome := _checkpoint(db, job, worker_id):
                return outcome

            files = compare.get("files", [])
            if len(files) >= COMPARE_FILE_CAP:
                return _fail_or_lost(
                    db,
                    job,
                    worker_id,
                    TOO_MANY_FILES_MESSAGE,
                    "compare files[] truncated at GitHub's 300-file cap",
                )
            diff_text = build_diff_text(files)
            with tempfile.TemporaryDirectory(prefix="difflens-review-") as tmp:
                workspace = Path(tmp)
                populate_workspace(client, repository.full_name, review.head_sha, files, workspace)
                if outcome := _checkpoint(db, job, worker_id):
                    return outcome

                result = run_review(
                    AnalysisJob(
                        repo_full_name=repository.full_name,
                        pr_title=pull.title,
                        base_sha=review.base_sha,
                        head_sha=review.head_sha,
                        diff_text=diff_text,
                        workspace=workspace,
                        mode="deterministic_only",
                    )
                )

        if not _persist_success(db, job, review, result, worker_id):
            log.warning("review_result_discarded_ownership_lost")
            return "lost"
        log.info("review_completed", findings=len(result.findings))
        return "completed"

    except GitHubAuthError:
        db.rollback()
        db.execute(
            update(ProviderConnection)
            .where(ProviderConnection.user_id == review.user_id)
            .values(token_invalid=True)
        )
        return _fail_or_lost(
            db, job, worker_id, RECONNECT_MESSAGE, "github rejected the token mid-review"
        )
    except GitHubNotFound:
        db.rollback()
        return _fail_or_lost(
            db, job, worker_id, SNAPSHOT_GONE_MESSAGE, "compare or contents returned 404"
        )
    except AnalysisError as exc:
        db.rollback()
        return _fail_or_lost(db, job, worker_id, BAD_DIFF_MESSAGE, str(exc))
    except GitHubRateLimited as exc:
        db.rollback()
        delay = 60 if exc.reset_at is None else max(exc.reset_at - int(jobs._now().timestamp()), 1)
        return _retry_or_fail(db, job, worker_id, TRANSIENT_MESSAGE, "github rate limited", delay)
    except Exception as exc:  # GitHubTransient and anything unforeseen: retryable
        db.rollback()
        log.warning("review_attempt_failed", error=f"{type(exc).__name__}: {exc}")
        return _retry_or_fail(
            db, job, worker_id, TRANSIENT_MESSAGE, f"{type(exc).__name__}: {exc}", 0
        )


def _fail_or_lost(
    db: Session, job: ReviewJob, worker_id: str, error_user: str, error_detail: str
) -> str:
    return "failed" if jobs.fail_job(db, job, worker_id, error_user, error_detail) else "lost"


def _retry_or_fail(
    db: Session,
    job: ReviewJob,
    worker_id: str,
    error_user: str,
    error_detail: str,
    min_delay_s: float,
) -> str:
    if job.attempts >= job.max_attempts:
        return _fail_or_lost(db, job, worker_id, EXHAUSTED_MESSAGE, error_detail)
    if jobs.requeue_for_retry(db, job, worker_id, error_user, error_detail, min_delay_s):
        return "retried"
    return "lost"
