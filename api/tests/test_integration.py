"""The whole loop, once, with nothing faked except GitHub.

Every other test in this suite isolates a layer. This one refuses to: it
posts through the HTTP API, lets the review reach Redis, pops it back off the
way the worker's loop does, runs the real pipeline over a real fixture diff,
and reads the findings back out through the API the browser calls. If the
seams between those pieces stop lining up, only this test notices.

Redis is real here on purpose. It is stubbed everywhere else, which means
nothing else would catch a doorbell that is rung with the wrong id.
"""

import json
import uuid
from pathlib import Path

import httpx
import pytest
from sqlalchemy import select

from app import queue
from app.models import Review, ReviewJob
from tests.conftest import BASE_SHA, HEAD_SHA_41, wire_fixture
from worker import runner

FIXTURES = Path(__file__).parent / "fixtures"
WORKER_ID = "integration-worker"


@pytest.fixture
def doorbell():
    """A real Redis list, emptied first.

    Other tests push real job ids onto the same key and nothing rolls Redis
    back, so without the drain this test could pop a stranger's job id.
    """
    try:
        client = queue.get_redis()
        client.delete(queue.QUEUE_KEY)
    except Exception as exc:  # pragma: no cover - only when Redis is absent
        pytest.skip(f"integration test needs a live Redis: {exc}")
    return client


@pytest.fixture
def signed_in_pull(client, github, make_user_with_session):
    """Alice, signed in, with octocat/alpha PR #41 synced and wired to a fixture."""
    make_user_with_session("alice")
    repos = client.get("/repositories").json()["items"]
    repo_id = next(item["id"] for item in repos if item["full_name"] == "octocat/alpha")
    pulls = client.get(f"/repositories/{repo_id}/pull-requests").json()["items"]
    pull_id = next(item["id"] for item in pulls if item["number"] == 41)
    wire_fixture(github, "python_buggy")
    return pull_id


def _drain_one(doorbell) -> str:
    job_id = queue.wait_for_job(doorbell, timeout_s=5)
    assert job_id, "the doorbell never rang for the review that was just created"
    return job_id


def test_a_review_runs_end_to_end_and_reads_back_through_the_api(
    client, db, doorbell, signed_in_pull
):
    expected = json.loads((FIXTURES / "python_buggy" / "expected.json").read_text())

    created = client.post("/reviews", json={"pull_request_id": signed_in_pull})
    assert created.status_code == 201
    review_id = created.json()["id"]
    assert created.json()["status"] == "queued"
    assert created.json()["findings"] == []

    # Queued means queued: readable, and honest that there is nothing yet
    pending = client.get(f"/reviews/{review_id}").json()
    assert pending["status"] == "queued"
    assert pending["findings_count"] in (0, None)

    job = db.execute(
        select(ReviewJob).where(ReviewJob.review_id == uuid.UUID(review_id))
    ).scalar_one()
    assert _drain_one(doorbell) == str(job.id), "the doorbell rang for a different job"

    assert runner.process_job(db, job.id, WORKER_ID) == "completed"

    done = client.get(f"/reviews/{review_id}").json()
    assert done["status"] == "completed"
    assert done["findings_count"] == len(expected)
    assert done["summary"]
    assert done["error_user_message"] is None
    assert len(done["findings"]) == len(expected)

    # The payload the review page actually renders
    assert done["pull_request"]["number"] == 41
    assert done["pull_request"]["repository_full_name"] == "octocat/alpha"
    assert sum(done["severity_counts"].values()) == len(expected)
    for finding in done["findings"]:
        assert finding["file_path"]
        assert finding["start_line"] >= 1
        assert finding["end_line"] >= finding["start_line"]
        assert finding["severity"] in {"critical", "high", "medium", "low", "info"}
        assert finding["source"] in {"deterministic", "ai", "hybrid"}
        assert finding["feedback"] is None

    # ...and the feedback the page writes back
    target = done["findings"][0]["id"]
    assert client.put(f"/findings/{target}/feedback", json={"verdict": "useful"}).status_code == 200
    after = client.get(f"/reviews/{review_id}").json()
    assert {f["id"]: f["feedback"] for f in after["findings"]}[target] == "useful"

    assert client.delete(f"/findings/{target}/feedback").status_code == 200
    cleared = client.get(f"/reviews/{review_id}").json()
    assert {f["id"]: f["feedback"] for f in cleared["findings"]}[target] is None


def test_a_transient_github_failure_is_retried_and_then_succeeds(
    client, db, doorbell, github, signed_in_pull
):
    """The retry path, driven through the API rather than around it.

    A free-tier deploy hits GitHub 5xx and timeouts routinely, so "the first
    attempt failed" has to end in findings, not in a dead review.
    """
    compare_path = f"/repos/octocat/alpha/compare/{BASE_SHA}...{HEAD_SHA_41}"
    github.responses[compare_path] = httpx.Response(502, json={"message": "bad gateway"})

    created = client.post("/reviews", json={"pull_request_id": signed_in_pull})
    review_id = created.json()["id"]
    job = db.execute(
        select(ReviewJob).where(ReviewJob.review_id == uuid.UUID(review_id))
    ).scalar_one()
    _drain_one(doorbell)

    assert runner.process_job(db, job.id, WORKER_ID) == "retried"

    # The user is told it is still going, not that it broke
    mid = client.get(f"/reviews/{review_id}").json()
    assert mid["status"] == "queued"
    assert mid["error_user_message"] is None

    del github.responses[compare_path]  # GitHub comes back
    job.run_after = job.created_at  # the sweep's backoff, skipped forward
    db.flush()
    assert runner.process_job(db, job.id, WORKER_ID) == "completed"

    done = client.get(f"/reviews/{review_id}").json()
    assert done["status"] == "completed"
    assert done["findings_count"] > 0
    db.refresh(job)
    assert job.attempts == 2, "the retry was not counted"


def test_a_second_review_of_the_same_commit_is_refused_then_rerunnable(
    client, db, doorbell, signed_in_pull
):
    """The 409-then-rerun path the Run review button depends on."""
    first = client.post("/reviews", json={"pull_request_id": signed_in_pull})
    review_id = first.json()["id"]
    job = db.execute(
        select(ReviewJob).where(ReviewJob.review_id == uuid.UUID(review_id))
    ).scalar_one()
    _drain_one(doorbell)
    assert runner.process_job(db, job.id, WORKER_ID) == "completed"

    clash = client.post("/reviews", json={"pull_request_id": signed_in_pull})
    assert clash.status_code == 409
    assert clash.json()["error"]["code"] == "review_already_exists"
    assert clash.json()["error"]["review_id"] == review_id

    again = client.post(f"/reviews/{review_id}/rerun")
    assert again.status_code == 201
    assert again.json()["id"] != review_id
    assert again.json()["status"] == "queued"

    superseded = db.get(Review, uuid.UUID(review_id))
    db.refresh(superseded)
    assert superseded.status == "superseded"
    # The superseded review keeps its findings and stays readable
    old = client.get(f"/reviews/{review_id}").json()
    assert old["status"] == "superseded"
    assert old["findings"]

    fresh_job = db.execute(
        select(ReviewJob).where(ReviewJob.review_id == uuid.UUID(again.json()["id"]))
    ).scalar_one()
    assert _drain_one(doorbell) == str(fresh_job.id)
    assert runner.process_job(db, fresh_job.id, WORKER_ID) == "completed"
