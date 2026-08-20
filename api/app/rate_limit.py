"""Fixed-window request limits, counted in Redis.

Only the expensive endpoints are limited. Starting a review spends GitHub API
quota, worker time, and AI tokens, so it is the one thing a signed-in account
can do that costs real money; reads are Postgres-bound and cheap.

Two deliberate properties, both recorded as accepted gaps in
docs/THREAT_MODEL.md:

- The window is fixed, not sliding, so a caller who times a burst across a
  window boundary can briefly get twice the limit. The cost of that burst is
  bounded and the code is a tenth the size of a sliding log.
- The limiter fails OPEN. Redis is a doorbell in this system and never the
  source of truth; an Upstash outage must not stop reviews from being
  started. It logs when it does that, so an outage is visible rather than
  silently unlimited.
"""

import time
from dataclasses import dataclass
from typing import Annotated

import redis
import structlog
from fastapi import Depends, HTTPException

from app import queue
from app.config import settings
from app.deps import CurrentUser

log = structlog.get_logger()

KEY_PREFIX = "difflens:rl"


@dataclass(frozen=True)
class Limit:
    name: str
    max_requests: int
    window_s: int


def _window_key(limit: Limit, identity: str, now: float) -> str:
    # The window index is part of the key, so a window resets by expiring
    # rather than by anything having to reset it
    return f"{KEY_PREFIX}:{limit.name}:{identity}:{int(now) // limit.window_s}"


def _retry_after_s(limit: Limit, now: float) -> int:
    return max(limit.window_s - int(now) % limit.window_s, 1)


def describe_window(seconds: int) -> str:
    """The window as a person would say it."""
    if seconds % 3600 == 0 and seconds >= 3600:
        hours = seconds // 3600
        return "hour" if hours == 1 else f"{hours} hours"
    if seconds % 60 == 0 and seconds >= 60:
        minutes = seconds // 60
        return "minute" if minutes == 1 else f"{minutes} minutes"
    return f"{seconds} seconds"


def describe_limit(limit: Limit) -> str:
    """The whole sentence. This message is shown to the user verbatim by the
    frontend, so it has to read like English rather than like a counter."""
    reviews = "1 review" if limit.max_requests == 1 else f"{limit.max_requests} reviews"
    return f"You can start {reviews} every {describe_window(limit.window_s)}"


def too_many(limit: Limit, retry_after_s: int) -> HTTPException:
    return HTTPException(
        status_code=429,
        detail={
            "code": "rate_limited",
            "message": (
                f"Too many requests. {describe_limit(limit)}; try again in {retry_after_s}s."
            ),
        },
        headers={"Retry-After": str(retry_after_s)},
    )


def check(client: redis.Redis | None, limit: Limit, identity: str) -> None:
    """Count one request against a limit, raising 429 once it is exceeded."""
    if limit.max_requests <= 0:
        return  # 0 disables the limit, which is how tests and local dev opt out
    if client is None:
        # No queue configured at all; same answer as an outage, allow and say so
        log.warning("rate_limit_unavailable_allowing", limit=limit.name, identity=identity)
        return
    now = time.time()
    key = _window_key(limit, identity, now)
    try:
        pipe = client.pipeline()
        pipe.incr(key)
        # NX so a later hit in the same window cannot push the expiry out and
        # turn the fixed window into one that never resets under load
        pipe.expire(key, limit.window_s, nx=True)
        used = int(pipe.execute()[0])
    except redis.RedisError:
        log.warning("rate_limit_unavailable_allowing", limit=limit.name, identity=identity)
        return
    if used > limit.max_requests:
        retry_after = _retry_after_s(limit, now)
        log.info(
            "rate_limited",
            limit=limit.name,
            identity=identity,
            used=used,
            max_requests=limit.max_requests,
        )
        raise too_many(limit, retry_after)


def review_limit() -> Limit:
    # Read at call time, not import time, so tests can move the limit
    return Limit(
        name="reviews",
        max_requests=settings.review_rate_limit,
        window_s=settings.review_rate_limit_window_s,
    )


def enforce_review_rate_limit(user: CurrentUser) -> None:
    """Dependency for the endpoints that start work. Declare it before the
    GitHub client so a throttled caller never costs a GitHub round trip."""
    # Through the module, so anything that stubs the queue stubs this too
    check(queue.get_redis(), review_limit(), str(user.id))


ReviewRateLimit = Annotated[None, Depends(enforce_review_rate_limit)]
