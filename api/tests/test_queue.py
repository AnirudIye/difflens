"""The queue module is a doorbell, not a store: Postgres owns job state.

These tests run against the real Redis from docker-compose (db 1, pinned in
conftest) because the failure modes that matter (timeouts, dead connections)
only exist on a real client.
"""

import uuid

import pytest
import redis as redis_lib

from app import queue


@pytest.fixture
def redis_client():
    client = queue.get_redis()
    try:
        client.ping()
    except redis_lib.RedisError:
        pytest.fail("Redis is not reachable; run `docker compose up -d` first")
    client.delete(queue.QUEUE_KEY)
    yield client
    client.delete(queue.QUEUE_KEY)


def _broken_client() -> redis_lib.Redis:
    # Port 1 is never a Redis; connect fails fast
    return redis_lib.Redis(
        host="localhost", port=1, socket_connect_timeout=0.05, socket_timeout=0.05
    )


def test_notify_then_wait_roundtrip(redis_client):
    job_id = uuid.uuid4()
    assert queue.notify(redis_client, job_id) is True
    assert queue.wait_for_job(redis_client, timeout_s=1) == str(job_id)


def test_wait_returns_none_on_timeout(redis_client):
    assert queue.wait_for_job(redis_client, timeout_s=1) is None


def test_doorbell_is_fifo(redis_client):
    first, second = uuid.uuid4(), uuid.uuid4()
    queue.notify(redis_client, first)
    queue.notify(redis_client, second)
    assert queue.wait_for_job(redis_client, timeout_s=1) == str(first)
    assert queue.wait_for_job(redis_client, timeout_s=1) == str(second)


def test_notify_never_raises_when_redis_is_down():
    # The API rings the doorbell after commit; a dead Redis must not fail the
    # request, the sweep recovers the job instead.
    assert queue.notify(_broken_client(), uuid.uuid4()) is False


def test_wait_sleeps_out_the_timeout_when_redis_is_down(monkeypatch):
    # A dead Redis must not hot-spin the worker loop: the wait degrades to a
    # sleep so the loop keeps its sweep cadence and its command budget.
    sleeps: list[float] = []
    monkeypatch.setattr(queue.time, "sleep", lambda seconds: sleeps.append(seconds))
    assert queue.wait_for_job(_broken_client(), timeout_s=25) is None
    assert sleeps == [25]
