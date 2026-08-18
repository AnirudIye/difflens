"""The review_jobs state machine: every transition is one guarded UPDATE.

Postgres is the source of truth. Claims, retries, and the sweep all compete
through atomic UPDATE ... WHERE guards, so duplicate doorbells and racing
workers are harmless: at most one of them wins the row.
"""

import uuid
from datetime import UTC, datetime, timedelta

import redis
import structlog
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app import queue
from app.models import Review, ReviewJob

log = structlog.get_logger()

# A worker heartbeats every 10s; two missed minutes means it is gone
HEARTBEAT_INTERVAL_S = 10
STALE_AFTER_S = 120
SWEEP_INTERVAL_S = 30
RETRY_BACKOFF_BASE_S = 30


def _now() -> datetime:
    return datetime.now(UTC)


def claim_job(db: Session, job_id: uuid.UUID, worker_id: str) -> ReviewJob | None:
    """Atomically move one queued, due job to running. None if someone beat us."""
    now = _now()
    claimed = db.execute(
        update(ReviewJob)
        .where(
            ReviewJob.id == job_id,
            ReviewJob.status == "queued",
            ReviewJob.run_after <= now,
        )
        .values(
            status="running",
            attempts=ReviewJob.attempts + 1,
            locked_by=worker_id,
            locked_at=now,
            heartbeat_at=now,
            started_at=now,
        )
        .returning(ReviewJob.review_id)
    ).first()
    if claimed is None:
        db.rollback()
        return None
    db.execute(
        update(Review)
        .where(Review.id == claimed.review_id)
        .values(status="running", started_at=now)
    )
    db.commit()
    job = db.get(ReviewJob, job_id)
    if job is not None:
        db.refresh(job)
    return job


def record_heartbeat(db: Session, job_id: uuid.UUID, worker_id: str) -> bool:
    """Bump heartbeat_at. False means the job is no longer ours to beat for."""
    beaten = db.execute(
        update(ReviewJob)
        .where(
            ReviewJob.id == job_id,
            ReviewJob.status == "running",
            ReviewJob.locked_by == worker_id,
        )
        .values(heartbeat_at=_now())
        .returning(ReviewJob.id)
    ).first()
    db.commit()
    return beaten is not None


def retry_backoff(attempts: int) -> timedelta:
    return timedelta(seconds=RETRY_BACKOFF_BASE_S * 2 ** max(attempts - 1, 0))


def _transition_owned(
    db: Session,
    job: ReviewJob,
    worker_id: str,
    transition: str,
    job_values: dict,
    review_values: dict,
) -> bool:
    """Apply a running-job transition only if this worker still owns the row.

    The ownership fence (status='running' AND locked_by=worker_id) is what
    makes a sweep-reclaimed zombie worker harmless: its late writes match
    zero rows and the current owner's state stands. The review update only
    fires when the job fence won, so the pair can never diverge.
    """
    won = db.execute(
        update(ReviewJob)
        .where(
            ReviewJob.id == job.id,
            ReviewJob.status == "running",
            ReviewJob.locked_by == worker_id,
        )
        .values(**job_values)
        .returning(ReviewJob.review_id)
    ).first()
    if won is None:
        db.rollback()
        log.warning("job_transition_lost", job_id=str(job.id), transition=transition)
        return False
    db.execute(update(Review).where(Review.id == won.review_id).values(**review_values))
    db.commit()
    db.refresh(job)
    return True


def requeue_for_retry(
    db: Session,
    job: ReviewJob,
    worker_id: str,
    error_user: str,
    error_detail: str,
    min_delay_s: float = 0,
) -> bool:
    """Put a running job back in the queue with exponential backoff.

    The doorbell is NOT rung here: the delay lives in run_after and the sweep
    (or the loop's Postgres fallback) picks the job up once it comes due.
    """
    now = _now()
    delay = max(retry_backoff(job.attempts), timedelta(seconds=min_delay_s))
    return _transition_owned(
        db,
        job,
        worker_id,
        "requeue",
        dict(
            status="queued",
            run_after=now + delay,
            locked_by=None,
            locked_at=None,
            heartbeat_at=None,
            error_user=error_user,
            error_detail=error_detail,
        ),
        dict(status="queued", started_at=None),
    )


def fail_job(
    db: Session, job: ReviewJob, worker_id: str, error_user: str, error_detail: str
) -> bool:
    now = _now()
    return _transition_owned(
        db,
        job,
        worker_id,
        "fail",
        dict(status="failed", finished_at=now, error_user=error_user, error_detail=error_detail),
        dict(status="failed", error_user_message=error_user, completed_at=now),
    )


def cancel_job(db: Session, job: ReviewJob, worker_id: str) -> bool:
    now = _now()
    return _transition_owned(
        db,
        job,
        worker_id,
        "cancel",
        dict(status="cancelled", cancel_requested=True, finished_at=now),
        dict(status="cancelled", completed_at=now),
    )


def next_due_job_id(db: Session) -> uuid.UUID | None:
    """The oldest due queued job, if any: the Postgres-only dispatch path.

    This is what makes 'Redis loss only delays reviews' true rather than
    aspirational: the loop asks Postgres directly whenever the doorbell
    yields nothing, so a dead Redis degrades to 25s-latency polling.
    """
    return db.execute(
        select(ReviewJob.id)
        .where(ReviewJob.status == "queued", ReviewJob.run_after <= _now())
        .order_by(ReviewJob.run_after)
        .limit(1)
    ).scalar_one_or_none()


CRASH_MESSAGE = "The review crashed too many times and was stopped"


def sweep(db: Session, redis_client: redis.Redis | None) -> dict[str, int]:
    """Reconcile Postgres truth with reality; ring for anything runnable.

    Recovers from every dropped-message and dead-worker case: a lost doorbell,
    a crashed worker mid-review, a cancel that raced the queue. Runs on every
    worker between pops; all statements are guarded, so overlapping sweeps
    from several workers cannot double-apply.
    """
    now = _now()
    counts = {"cancelled": 0, "requeued": 0, "failed": 0, "rung": 0}

    # Cancels that landed while the job still sat in the queue
    cancelled = db.execute(
        update(ReviewJob)
        .where(ReviewJob.status == "queued", ReviewJob.cancel_requested.is_(True))
        .values(status="cancelled", finished_at=now)
        .returning(ReviewJob.review_id)
    ).all()
    if cancelled:
        db.execute(
            update(Review)
            .where(Review.id.in_([row.review_id for row in cancelled]))
            .values(status="cancelled", completed_at=now)
        )
    counts["cancelled"] = len(cancelled)

    # Workers that died mid-review: heartbeat gone stale
    stale_cutoff = now - timedelta(seconds=STALE_AFTER_S)
    requeued = db.execute(
        update(ReviewJob)
        .where(
            ReviewJob.status == "running",
            ReviewJob.heartbeat_at < stale_cutoff,
            ReviewJob.attempts < ReviewJob.max_attempts,
        )
        .values(
            status="queued",
            run_after=now,
            locked_by=None,
            locked_at=None,
            heartbeat_at=None,
            error_detail="requeued by sweep: worker heartbeat went stale",
        )
        .returning(ReviewJob.review_id)
    ).all()
    if requeued:
        db.execute(
            update(Review)
            .where(Review.id.in_([row.review_id for row in requeued]))
            .values(status="queued", started_at=None)
        )
    counts["requeued"] = len(requeued)

    failed = db.execute(
        update(ReviewJob)
        .where(
            ReviewJob.status == "running",
            ReviewJob.heartbeat_at < stale_cutoff,
            ReviewJob.attempts >= ReviewJob.max_attempts,
        )
        .values(
            status="failed",
            finished_at=now,
            error_user=CRASH_MESSAGE,
            error_detail="failed by sweep: heartbeat stale with no attempts left",
        )
        .returning(ReviewJob.review_id)
    ).all()
    if failed:
        db.execute(
            update(Review)
            .where(Review.id.in_([row.review_id for row in failed]))
            .values(status="failed", error_user_message=CRASH_MESSAGE, completed_at=now)
        )
    counts["failed"] = len(failed)

    db.commit()

    # Ring for everything due. Duplicate rings are harmless: the claim's
    # UPDATE guard makes the second pop a no-op. Only delivered rings are
    # counted, so an operator reading sweep_acted during a Redis outage sees
    # the truth (the loop's Postgres fallback still runs these jobs).
    due = db.execute(
        select(ReviewJob.id).where(ReviewJob.status == "queued", ReviewJob.run_after <= now)
    ).scalars()
    for job_id in due:
        if queue.notify(redis_client, job_id):  # type: ignore[arg-type]
            counts["rung"] += 1

    if any(counts.values()):
        log.info("sweep_acted", **counts)
    return counts
