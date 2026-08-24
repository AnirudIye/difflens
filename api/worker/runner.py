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

import tarfile
import tempfile
import uuid
from collections import Counter
from pathlib import Path

import anthropic
import structlog
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.ai.errors import AIProviderConfigError, UserAIKeyError
from app.ai.factory import resolve_provider
from app.ai.mock import MockProvider
from app.analysis.ai_review import DEMO_AI_MODEL
from app.analysis.models import ReviewJob as AnalysisJob
from app.analysis.models import ReviewResult
from app.analysis.pipeline import AnalysisError, run_review
from app.analysis.repo_review import KEYLESS_AI_CHUNKS, MAX_AI_CHUNKS, AnalysisStopped
from app.demo import sample as demo_sample
from app.demo.candidates import DEMO_CANDIDATES
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
    GitHubSnapshotTooLarge,
)
from worker import jobs
from worker.snapshot import SnapshotTooLarge, extract_snapshot

log = structlog.get_logger()

RECONNECT_MESSAGE = "GitHub access is missing or was revoked; reconnect and run again"
AI_MISCONFIGURED_MESSAGE = "The AI reviewer is misconfigured; ask the operator to check settings"
BYOK_MISCONFIGURED_MESSAGE = "Your AI API key failed; check it in Settings and run the review again"

# Provider failures that no retry can fix: a bad key, a bad model id, or a
# request the API rejects outright
AI_CONFIG_ERRORS = (
    AIProviderConfigError,
    anthropic.AuthenticationError,
    anthropic.PermissionDeniedError,
    anthropic.NotFoundError,
    anthropic.BadRequestError,
)
SNAPSHOT_GONE_MESSAGE = "The reviewed commits are no longer reachable on GitHub"
BAD_DIFF_MESSAGE = "GitHub returned a diff this pipeline could not parse"
TRANSIENT_MESSAGE = "The review hit a temporary problem; it will retry shortly"
EXHAUSTED_MESSAGE = "The review kept failing and was stopped; run it again to retry"
TOO_MANY_FILES_MESSAGE = (
    "This pull request changes too many files to review (GitHub caps the comparison at 300 files)"
)
REPO_TOO_LARGE_MESSAGE = (
    "This repository snapshot is too large to review (over 20,000 files or 200 MB of content)"
)
BAD_SNAPSHOT_MESSAGE = "GitHub returned a repository snapshot this pipeline could not unpack"

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
    db: Session, job: ReviewJob, review: Review, result: ReviewResult, worker_id: str, mode: str
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
    stats = result.stats
    markers = [
        marker
        for flag, marker in (
            (stats.ai_refused, "ai_refused"),
            (stats.ai_parse_failed, "ai_parse_failed"),
            (stats.ai_truncated, "ai_truncated"),
            (stats.ai_skipped, f"ai_skipped={stats.ai_skipped}"),
            (stats.truncated, "findings_truncated"),
            (
                stats.analyzers_skipped,
                f"analyzers_skipped={','.join(sorted(stats.analyzers_skipped))}",
            ),
            (
                review.repository_id is not None,
                f"ai_coverage={stats.ai_files_covered}/{stats.ai_files_total}",
            ),
            (stats.ai_capped, "ai_capped=keyless"),
            (stats.ai_chunks_failed, f"ai_chunks_failed={stats.ai_chunks_failed}"),
        )
        if flag
    ]
    pipeline_version = " ".join(
        [mode]
        + [f"{tool}={version}" for tool, version in sorted(stats.tool_versions.items())]
        + markers
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


def _run_demo_review(review: Review, pull: PullRequest, repository: Repository) -> ReviewResult:
    """Run the bundled demo pull request. No GitHub, no provider key, no cost.

    Deliberately takes no session and no job: it touches nothing but the
    filesystem and the pure pipeline, so the caller keeps every cancellation
    and ownership decision in one place. An earlier version checkpointed in
    here and signalled early exit by returning None, which made the caller
    ask _checkpoint a second time; by then cancel_job had already moved the
    row out of "running", so a cancelled demo review reported itself as
    "lost".

    The diff and the workspace come from app/demo/sample/, which ships in the
    image; api/tests/ does not. Everything after that is the ordinary
    pipeline, so the demo exercises the same analyzers, the same validation
    chain, and the same dedup as a real review rather than copies of them.
    """
    with tempfile.TemporaryDirectory(prefix="difflens-demo-") as tmp:
        workspace = Path(tmp)
        demo_sample.populate_workspace(workspace)
        return run_review(
            AnalysisJob(
                repo_full_name=repository.full_name,
                pr_title=pull.title,
                base_sha=review.base_sha,
                head_sha=review.head_sha,
                diff_text=demo_sample.build_diff(),
                workspace=workspace,
                mode="demo",
            ),
            provider=MockProvider(DEMO_CANDIDATES, model=DEMO_AI_MODEL),
        )


def _github_connection(db: Session, user_id: uuid.UUID) -> ProviderConnection | None:
    return db.execute(
        select(ProviderConnection).where(
            ProviderConnection.user_id == user_id,
            ProviderConnection.provider == "github",
        )
    ).scalar_one_or_none()


def _run_repository_review(
    db: Session,
    job: ReviewJob,
    review: Review,
    repository: Repository,
    worker_id: str,
    mode: str,
    ai_provider,
    ai_source: str,
) -> ReviewResult | str:
    """Ingest the snapshot tarball and run the pipeline over the whole tree.

    Returns the result, or a checkpoint outcome string when the job was
    cancelled or lost between stages. The chunk cap is the tier decision:
    a user's own key buys full coverage, the shared keyless tier gets one
    chunk and an honest note saying so.
    """
    chunk_cap = MAX_AI_CHUNKS if ai_source == "user" else KEYLESS_AI_CHUNKS
    cap_reason = None if ai_source == "user" else "keyless"
    connection = _github_connection(db, review.user_id)
    assert connection is not None  # caller checked
    with GitHubClient(decrypt_token(connection.access_token_enc)) as client:
        with tempfile.TemporaryDirectory(prefix="difflens-repo-") as tmp:
            tmp_path = Path(tmp)
            tar_path = tmp_path / "snapshot.tar.gz"
            workspace = tmp_path / "workspace"
            workspace.mkdir()
            client.download_tarball(repository.full_name, review.head_sha, tar_path)
            if outcome := _checkpoint(db, job, worker_id):
                return outcome
            extract_snapshot(tar_path, workspace)
            # The tarball is spent; reclaim the disk before analyzers run
            tar_path.unlink()
            if outcome := _checkpoint(db, job, worker_id):
                return outcome
            return run_review(
                AnalysisJob(
                    repo_full_name=repository.full_name,
                    base_sha=None,
                    head_sha=review.head_sha,
                    diff_text="",
                    workspace=workspace,
                    mode=mode,  # type: ignore[arg-type]  # factory returns a valid mode
                    target="repository",
                    ai_chunk_cap=chunk_cap,
                    ai_cap_reason=cap_reason,
                ),
                provider=ai_provider,
                stop_check=lambda: _checkpoint(db, job, worker_id),
                ai_config_errors=AI_CONFIG_ERRORS,
            )


def run_claimed_job(db: Session, job: ReviewJob, worker_id: str) -> str:
    """Run a job this worker already claimed (the loop claims before it
    starts the heartbeat thread, so a beat can never race its own claim)."""
    review = db.get(Review, job.review_id)
    assert review is not None  # FK guarantees it; reviews are never hard-deleted
    log = structlog.get_logger().bind(job_id=str(job.id), review_id=str(review.id))
    ai_source = "server"  # refined by resolve_provider below

    try:
        if outcome := _checkpoint(db, job, worker_id):
            return outcome

        if review.pull_request_id is None:
            repository = db.get(Repository, review.repository_id)
            assert repository is not None
            # The demo divert stays PR-only; creation cannot reach the demo
            # repository through the authenticated path, and this assert
            # keeps a future seeding change from quietly changing that
            assert not repository.is_demo
            try:
                mode, ai_provider, ai_source = resolve_provider(db, review.user_id)
            except UserAIKeyError as exc:
                return _fail_or_lost(db, job, worker_id, BYOK_MISCONFIGURED_MESSAGE, str(exc))
            except ValueError as exc:
                return _fail_or_lost(db, job, worker_id, AI_MISCONFIGURED_MESSAGE, str(exc))
            connection = _github_connection(db, review.user_id)
            if connection is None or connection.token_invalid:
                return _fail_or_lost(
                    db, job, worker_id, RECONNECT_MESSAGE, "github connection missing or invalid"
                )
            outcome_or_result = _run_repository_review(
                db, job, review, repository, worker_id, mode, ai_provider, ai_source
            )
            if isinstance(outcome_or_result, str):
                return outcome_or_result
            result = outcome_or_result
        else:
            pull = db.get(PullRequest, review.pull_request_id)
            assert pull is not None
            assert review.base_sha is not None  # ck_reviews_pr_has_base guarantees it
            repository = db.get(Repository, pull.repository_id)
            assert repository is not None

            if repository.is_demo:
                # The public demo, decided before the provider is resolved and
                # before any token is looked up. Entering this branch first is
                # what guarantees a demo review can reach neither GitHub nor
                # the operator's AI key, whoever started it.
                mode = "demo"
                if outcome := _checkpoint(db, job, worker_id):
                    return outcome
                result = _run_demo_review(review, pull, repository)
            else:
                # Resolve the AI provider before spending any GitHub calls:
                # the review author's own key wins, then server config. A
                # config error is permanent and burns no retries.
                try:
                    mode, ai_provider, ai_source = resolve_provider(db, review.user_id)
                except UserAIKeyError as exc:
                    # Only the user can fix their unreadable key; say so
                    return _fail_or_lost(db, job, worker_id, BYOK_MISCONFIGURED_MESSAGE, str(exc))
                except ValueError as exc:
                    return _fail_or_lost(db, job, worker_id, AI_MISCONFIGURED_MESSAGE, str(exc))

                connection = _github_connection(db, review.user_id)
                if connection is None or connection.token_invalid:
                    return _fail_or_lost(
                        db,
                        job,
                        worker_id,
                        RECONNECT_MESSAGE,
                        "github connection missing or invalid",
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
                        populate_workspace(
                            client, repository.full_name, review.head_sha, files, workspace
                        )
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
                                mode=mode,  # type: ignore[arg-type]  # factory returns a valid mode
                            ),
                            provider=ai_provider,
                        )

        stats = result.stats
        if stats.ai_refused or stats.ai_parse_failed or stats.ai_truncated or stats.ai_skipped:
            # An attacker-authored diff can suppress the AI stage; make sure
            # that never happens silently
            log.warning(
                "ai_stage_degraded",
                refused=stats.ai_refused,
                parse_failed=stats.ai_parse_failed,
                truncated=stats.ai_truncated,
                skipped=stats.ai_skipped,
            )
        if not _persist_success(db, job, review, result, worker_id, mode):
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
    except AnalysisStopped as exc:
        # Cancellation noticed between AI chunks; _checkpoint already moved
        # the job, so the outcome just propagates
        return exc.outcome
    except (SnapshotTooLarge, GitHubSnapshotTooLarge) as exc:
        # Permanent: the repository will be exactly as big on every retry
        db.rollback()
        return _fail_or_lost(db, job, worker_id, REPO_TOO_LARGE_MESSAGE, str(exc))
    except (tarfile.ReadError, EOFError) as exc:
        db.rollback()
        return _fail_or_lost(
            db, job, worker_id, BAD_SNAPSHOT_MESSAGE, f"{type(exc).__name__}: {exc}"
        )
    except AI_CONFIG_ERRORS as exc:
        # A revoked key or bad model id fails every retry identically; stop
        # now, and blame the key's owner accurately
        db.rollback()
        message = BYOK_MISCONFIGURED_MESSAGE if ai_source == "user" else AI_MISCONFIGURED_MESSAGE
        return _fail_or_lost(db, job, worker_id, message, f"{type(exc).__name__}: {exc}")
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
