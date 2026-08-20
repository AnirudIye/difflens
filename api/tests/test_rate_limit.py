"""The review rate limit: a fixed window counted in Redis, failing open.

Both halves are tested, because both are decisions someone will question:
the limit has to actually stop the 21st review, and a Redis outage has to
let reviews through rather than taking the product down with the doorbell.
"""

import uuid
from typing import cast

import pytest
import redis
from fastapi import HTTPException

from app import queue, rate_limit
from app.config import settings
from app.rate_limit import Limit, check


class FakeRedis:
    """Just enough Redis to count. A real client is used by the endpoint
    tests below; this one is for the branches a real client cannot reach."""

    def __init__(self, fail: bool = False):
        self.fail = fail
        self.counts: dict[str, int] = {}
        self.expiries: dict[str, int] = {}
        self.commands: list[tuple] = []

    def pipeline(self):
        return _FakePipeline(self)


class _FakePipeline:
    def __init__(self, store: FakeRedis):
        self.store = store
        self.queued: list[tuple] = []

    def incr(self, key):
        self.queued.append(("incr", key))
        return self

    def expire(self, key, seconds, nx=False):
        self.queued.append(("expire", key, seconds, nx))
        return self

    def execute(self):
        if self.store.fail:
            raise redis.ConnectionError("upstash is asleep")
        results = []
        for command in self.queued:
            self.store.commands.append(command)
            if command[0] == "incr":
                self.store.counts[command[1]] = self.store.counts.get(command[1], 0) + 1
                results.append(self.store.counts[command[1]])
            else:
                _, key, seconds, nx = command
                if not nx or key not in self.store.expiries:
                    self.store.expiries[key] = seconds
                results.append(True)
        return results


LIMIT = Limit(name="test", max_requests=3, window_s=60)


def count(client: FakeRedis | None, limit: Limit, identity: str) -> None:
    """rate_limit.check with the one cast the fake needs, kept in one place."""
    check(cast("redis.Redis | None", client), limit, identity)


def test_allows_up_to_the_limit_then_refuses():
    client = FakeRedis()
    identity = str(uuid.uuid4())

    for _ in range(LIMIT.max_requests):
        count(client, LIMIT, identity)

    with pytest.raises(HTTPException) as excinfo:
        count(client, LIMIT, identity)
    error = excinfo.value
    assert error.status_code == 429
    # detail is typed as a plain str upstream; this app always raises dicts
    assert cast("dict[str, str]", error.detail)["code"] == "rate_limited"
    assert error.headers is not None
    assert 1 <= int(error.headers["Retry-After"]) <= LIMIT.window_s


def test_counts_are_per_identity():
    client = FakeRedis()
    for _ in range(LIMIT.max_requests):
        count(client, LIMIT, "alice")
    count(client, LIMIT, "bob")  # bob is untouched by alice's spending


def test_expiry_is_set_once_so_the_window_actually_ends():
    client = FakeRedis()
    for _ in range(3):
        count(client, LIMIT, "alice")
    expires = [command for command in client.commands if command[0] == "expire"]
    assert all(command[3] is True for command in expires), "EXPIRE must be NX"
    assert len(set(client.expiries.values())) == 1


def test_a_new_window_starts_a_new_count(monkeypatch):
    client = FakeRedis()
    clock = [1_000_000.0]
    monkeypatch.setattr(rate_limit.time, "time", lambda: clock[0])

    for _ in range(LIMIT.max_requests):
        count(client, LIMIT, "alice")
    clock[0] += LIMIT.window_s  # over the boundary
    count(client, LIMIT, "alice")  # allowed again


def test_zero_disables_the_limit():
    client = FakeRedis()
    disabled = Limit(name="test", max_requests=0, window_s=60)
    for _ in range(50):
        count(client, disabled, "alice")
    assert client.counts == {}


def test_a_redis_outage_allows_the_request():
    # Redis is a doorbell in this system, never the source of truth. Losing it
    # must delay nothing and block nothing; docs/THREAT_MODEL.md accepts the
    # window of unlimited requests that this opens.
    for _ in range(50):
        count(FakeRedis(fail=True), LIMIT, "alice")


def test_no_queue_configured_allows_the_request():
    for _ in range(50):
        count(None, LIMIT, "alice")


# --- through the API ---


@pytest.fixture
def alice_pull(client, github, make_user_with_session):
    make_user_with_session("alice")
    repos = client.get("/repositories").json()["items"]
    repo_id = next(item["id"] for item in repos if item["full_name"] == "octocat/alpha")
    pulls = client.get(f"/repositories/{repo_id}/pull-requests").json()["items"]
    return {item["number"]: item["id"] for item in pulls}


def test_creating_reviews_is_limited(client, alice_pull, monkeypatch):
    monkeypatch.setattr(settings, "review_rate_limit", 1)

    first = client.post("/reviews", json={"pull_request_id": alice_pull[41]})
    assert first.status_code == 201

    second = client.post("/reviews", json={"pull_request_id": alice_pull[42]})
    assert second.status_code == 429
    body = second.json()["error"]
    assert body["code"] == "rate_limited"
    assert "request_id" in body
    assert int(second.headers["Retry-After"]) >= 1


def test_the_limit_is_checked_before_github_is_touched(client, alice_pull, github, monkeypatch):
    monkeypatch.setattr(settings, "review_rate_limit", 1)
    assert client.post("/reviews", json={"pull_request_id": alice_pull[41]}).status_code == 201

    before = len(github.calls)
    assert client.post("/reviews", json={"pull_request_id": alice_pull[42]}).status_code == 429
    assert len(github.calls) == before, "a throttled request still spent GitHub quota"


def test_reads_are_not_limited(client, alice_pull, monkeypatch):
    monkeypatch.setattr(settings, "review_rate_limit", 1)
    created = client.post("/reviews", json={"pull_request_id": alice_pull[41]})
    review_id = created.json()["id"]
    for _ in range(5):
        assert client.get(f"/reviews/{review_id}").status_code == 200


def test_the_limiter_uses_the_shared_queue_client(client, make_user_with_session, monkeypatch):
    """Regression guard for how the Redis client is reached.

    Binding get_redis by value at import time would make it impossible to
    stub, and every worker test that runs without Redis today would start
    needing a live one. Stubbing the queue module must stub the limiter.
    """
    user, _token = make_user_with_session("alice")
    asked = []
    monkeypatch.setattr(queue, "get_redis", lambda: asked.append("client") or None)

    rate_limit.enforce_review_rate_limit(user)

    assert asked == ["client"]


# --- the sentence a user actually reads ---


@pytest.mark.parametrize(
    ("max_requests", "window_s", "expected"),
    [
        (1, 3600, "You can start 1 review every hour"),
        (20, 3600, "You can start 20 reviews every hour"),
        (5, 7200, "You can start 5 reviews every 2 hours"),
        (3, 600, "You can start 3 reviews every 10 minutes"),
        (2, 60, "You can start 2 reviews every minute"),
        (4, 45, "You can start 4 reviews every 45 seconds"),
    ],
)
def test_the_limit_reads_like_english(max_requests, window_s, expected):
    # The frontend shows this message verbatim, so "1 reviews per 60 minutes"
    # would be shipped copy, not a log line
    assert rate_limit.describe_limit(Limit("reviews", max_requests, window_s)) == expected
