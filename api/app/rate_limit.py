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

import ipaddress
import time
from dataclasses import dataclass
from typing import Annotated

import redis
import structlog
from fastapi import Depends, HTTPException, Request

from app import queue
from app.config import settings
from app.deps import CurrentUser

log = structlog.get_logger()

KEY_PREFIX = "difflens:rl"

# Longest possible textual address is an IPv6 with an embedded IPv4 and a
# zone id; 64 is comfortably above that and bounds the parse either way
MAX_FORWARDED_CHARS = 64


@dataclass(frozen=True)
class Limit:
    name: str
    max_requests: int
    window_s: int
    # What one counted request is, as the user hears it in the 429 sentence:
    # "start a review" for the endpoints that spend money, "send a message"
    # for the contact form. Defaults keep every existing call site verbatim.
    verb: str = "start"
    noun: str = "review"


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
    things = f"1 {limit.noun}" if limit.max_requests == 1 else f"{limit.max_requests} {limit.noun}s"
    return f"You can {limit.verb} {things} every {describe_window(limit.window_s)}"


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


def demo_limit() -> Limit:
    return Limit(
        name="demo",
        max_requests=settings.demo_rate_limit,
        window_s=settings.demo_rate_limit_window_s,
    )


def client_ip(request: Request) -> str:
    """Best-effort caller address for the anonymous demo limit.

    Behind Vercel's rewrite proxy the caller's address only reaches us in
    X-Forwarded-For, and any client can put whatever it likes at the front of
    that header. So the identity is spoofable, deliberately: the demo's real
    cap is the partial unique index uq_reviews_pr_sha_live, which permits one
    live review per pull request and commit and therefore one live demo job
    at a time, in Postgres, with no help from this function or from Redis.

    The value is nevertheless parsed as an address before it is used, because
    it becomes Redis key material on an endpoint that needs no session. Taken
    verbatim, one request could pin a megabyte of Redis for the length of the
    window, and no amount of it was ever rate limited because each distinct
    value is its own fixed-window bucket. Parsing bounds a key at 45 bytes
    and makes a bucket cost an address rather than a string. Rotating real
    addresses still costs the operator small keys, which is the residual
    recorded in docs/THREAT_MODEL.md.
    """
    forwarded = request.headers.get("X-Forwarded-For", "")
    # Only the first element can be the original caller; the rest are proxies
    first = forwarded.split(",", 1)[0].strip()
    peer = request.client.host if request.client else ""
    # The peer is validated too, not just the header. uvicorn runs its
    # proxy-headers middleware by default and, for a connection it considers
    # a trusted proxy, REPLACES request.client with whatever X-Forwarded-For
    # said. So the "socket peer" is not necessarily a socket peer, and a
    # fallback that trusted it would hand back the very value just rejected.
    for candidate in (first, peer):
        if len(candidate) > MAX_FORWARDED_CHARS:
            continue
        try:
            return str(ipaddress.ip_address(candidate))
        except ValueError:
            continue
    return "unknown"


def enforce_demo_rate_limit(request: Request) -> None:
    """Dependency for the demo rerun, which anyone can press without a session."""
    check(queue.get_redis(), demo_limit(), f"ip:{client_ip(request)}")


DemoRateLimit = Annotated[None, Depends(enforce_demo_rate_limit)]


def contact_limit() -> Limit:
    return Limit(
        name="contact",
        max_requests=settings.contact_rate_limit,
        window_s=settings.contact_rate_limit_window_s,
        verb="send",
        noun="message",
    )


def enforce_contact_rate_limit(request: Request) -> None:
    """Dependency for the contact form, which anyone can submit without a
    session. Same spoofable-by-design identity as the demo limit: the cost of
    a burst here is bounded rows in Postgres, not money."""
    check(queue.get_redis(), contact_limit(), f"ip:{client_ip(request)}")


ContactRateLimit = Annotated[None, Depends(enforce_contact_rate_limit)]
