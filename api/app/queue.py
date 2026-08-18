"""Redis dispatch for review jobs: Postgres owns job state, Redis is a doorbell.

A lost doorbell delays a review (the worker's sweep re-rings it); it never
loses one. Every function here is therefore allowed to fail quietly.
"""

import time
import uuid

import redis
import structlog

from app.config import settings

log = structlog.get_logger()

QUEUE_KEY = "difflens:review_jobs"

# 86,400s / 25s ≈ 3.5k idle BRPOP commands per day, comfortably inside
# Upstash's 10k/day free tier (docs/architecture.md carries the math)
BRPOP_TIMEOUT_S = 25


def get_redis() -> redis.Redis:
    return redis.Redis.from_url(
        settings.redis_url,
        decode_responses=True,
        socket_connect_timeout=5,
        socket_timeout=BRPOP_TIMEOUT_S + 5,
    )


def notify(client: redis.Redis, job_id: uuid.UUID | str) -> bool:
    """Ring the doorbell for a job. Best-effort: never raises."""
    try:
        client.lpush(QUEUE_KEY, str(job_id))
        return True
    except redis.RedisError:
        log.warning("queue_notify_failed", job_id=str(job_id))
        return False


def wait_for_job(client: redis.Redis, timeout_s: int = BRPOP_TIMEOUT_S) -> str | None:
    """Block until a job id arrives or the timeout passes.

    A dead Redis degrades to a plain sleep so the worker loop keeps its sweep
    cadence without hot-spinning; reviews then flow through the sweep alone.
    """
    try:
        popped = client.brpop([QUEUE_KEY], timeout=timeout_s)
    except redis.RedisError:
        log.warning("queue_wait_failed_sleeping", timeout_s=timeout_s)
        time.sleep(timeout_s)
        return None
    if popped is None:
        return None
    _key, job_id = popped
    return job_id if isinstance(job_id, str) else job_id.decode()
