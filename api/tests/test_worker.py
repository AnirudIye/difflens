"""The worker's job state machine and the end-to-end review run.

Postgres rows are the truth throughout: every test asserts row transitions,
never in-memory state. GitHub is the FakeGitHub from conftest; Redis is the
captured doorbell, so these tests run without a live queue.
"""

import json
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from sqlalchemy import select
from unidiff import PatchSet

from app import queue
from app.models import Finding, ProviderConnection, Review, ReviewJob
from tests.conftest import BASE_SHA, HEAD_SHA_41
from worker import jobs, runner

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(autouse=True)
def doorbell(monkeypatch):
    rings: list[str] = []
    monkeypatch.setattr(queue, "notify", lambda client, job_id: rings.append(str(job_id)) or True)
    monkeypatch.setattr(queue, "get_redis", lambda: None)
    return rings


@pytest.fixture
def review_and_job(client, db, github, make_user_with_session):
    """Alice's queued review of octocat/alpha PR #41, created through the API."""
    make_user_with_session("alice")
    sync = client.get("/repositories")
    repo_id = next(
        item["id"] for item in sync.json()["items"] if item["full_name"] == "octocat/alpha"
    )
    pulls = client.get(f"/repositories/{repo_id}/pull-requests")
    pr_id = next(item["id"] for item in pulls.json()["items"] if item["number"] == 41)
    created = client.post("/reviews", json={"pull_request_id": pr_id})
    assert created.status_code == 201
    review = db.get(Review, uuid.UUID(created.json()["id"]))
    job = db.execute(select(ReviewJob).where(ReviewJob.review_id == review.id)).scalar_one()
    return review, job


def _ago(seconds: float) -> datetime:
    return datetime.now(UTC) - timedelta(seconds=seconds)


def compare_payload(diff_text: str) -> dict:
    """Shape one fixture diff the way GitHub's compare endpoint would."""
    files = []
    for patched in PatchSet(diff_text):
        source = (patched.source_file or "")[2:]
        target = (patched.target_file or "")[2:]
        if patched.is_added_file:
            status, filename = "added", target
        elif patched.is_removed_file:
            status, filename = "removed", source
        else:
            status, filename = "modified", target
        patch = "".join(str(hunk) for hunk in patched).rstrip("\n")
        files.append({"filename": filename, "status": status, "patch": patch})
    return {"status": "ahead", "files": files}


def wire_fixture(github, name: str) -> dict:
    """Point the fake compare and contents endpoints at one golden fixture."""
    root = FIXTURES / name
    diff_text = (root / "pr.diff").read_text()
    payload = compare_payload(diff_text)
    github.compares[("octocat/alpha", BASE_SHA, HEAD_SHA_41)] = payload
    for file in (root / "files").rglob("*"):
        if file.is_file():
            rel = file.relative_to(root / "files").as_posix()
            github.contents[(rel, HEAD_SHA_41)] = file.read_bytes()
    return payload


# --- claim ---


def test_claim_transitions_job_and_review(db, review_and_job):
    review, job = review_and_job
    claimed = jobs.claim_job(db, job.id, "w1")
    assert claimed is not None
    db.refresh(job)
    db.refresh(review)
    assert job.status == "running"
    assert job.attempts == 1
    assert job.locked_by == "w1"
    assert job.locked_at is not None
    assert job.heartbeat_at is not None
    assert job.started_at is not None
    assert review.status == "running"
    assert review.started_at is not None


def test_claim_is_exclusive(db, review_and_job):
    _review, job = review_and_job
    assert jobs.claim_job(db, job.id, "w1") is not None
    assert jobs.claim_job(db, job.id, "w2") is None


def test_claim_respects_run_after(db, review_and_job):
    _review, job = review_and_job
    job.run_after = datetime.now(UTC) + timedelta(hours=1)
    db.flush()
    assert jobs.claim_job(db, job.id, "w1") is None


def test_claim_ignores_unknown_ids(db, review_and_job):
    assert jobs.claim_job(db, uuid.uuid4(), "w1") is None


# --- heartbeat ---


def test_heartbeat_bumps_only_for_the_owner(db, review_and_job):
    _review, job = review_and_job
    jobs.claim_job(db, job.id, "w1")
    db.refresh(job)
    first_beat = job.heartbeat_at

    assert jobs.record_heartbeat(db, job.id, "w1") is True
    db.refresh(job)
    assert job.heartbeat_at > first_beat

    assert jobs.record_heartbeat(db, job.id, "w2") is False


# --- retry and failure ---


def test_requeue_for_retry_backs_off_exponentially(db, review_and_job):
    review, job = review_and_job
    jobs.claim_job(db, job.id, "w1")
    before = datetime.now(UTC)
    jobs.requeue_for_retry(db, job, "w1", "GitHub did not respond", "boom")
    db.refresh(job)
    db.refresh(review)
    assert job.status == "queued"
    assert job.locked_by is None and job.heartbeat_at is None
    assert job.error_user == "GitHub did not respond"
    # attempts=1 -> 30s * 2^0
    assert timedelta(seconds=25) <= job.run_after - before <= timedelta(seconds=40)
    assert review.status == "queued"

    jobs.claim_job(db, job.id, "w1")  # blocked by run_after
    job.run_after = datetime.now(UTC)
    db.flush()
    assert jobs.claim_job(db, job.id, "w1") is not None
    before = datetime.now(UTC)
    jobs.requeue_for_retry(db, job, "w1", "GitHub did not respond", "boom again")
    db.refresh(job)
    # attempts=2 -> 30s * 2^1
    assert timedelta(seconds=55) <= job.run_after - before <= timedelta(seconds=70)


def test_fail_job_finalizes_job_and_review(db, review_and_job):
    review, job = review_and_job
    jobs.claim_job(db, job.id, "w1")
    jobs.fail_job(db, job, "w1", "Something broke", "trace")
    db.refresh(job)
    db.refresh(review)
    assert job.status == "failed"
    assert job.finished_at is not None
    assert job.error_user == "Something broke"
    assert job.error_detail == "trace"
    assert review.status == "failed"
    assert review.error_user_message == "Something broke"
    assert review.completed_at is not None


def test_cancel_job_finalizes_both(db, review_and_job):
    review, job = review_and_job
    jobs.claim_job(db, job.id, "w1")
    jobs.cancel_job(db, job, "w1")
    db.refresh(job)
    db.refresh(review)
    assert job.status == "cancelled"
    assert review.status == "cancelled"
    assert job.finished_at is not None
    assert review.completed_at is not None


# --- sweep ---


def test_sweep_requeues_stale_running_job(db, review_and_job, doorbell):
    review, job = review_and_job
    jobs.claim_job(db, job.id, "w1")
    job.heartbeat_at = _ago(jobs.STALE_AFTER_S + 60)
    db.flush()

    counts = jobs.sweep(db, None)
    db.refresh(job)
    db.refresh(review)
    assert counts["requeued"] == 1
    assert job.status == "queued"
    assert job.locked_by is None
    assert review.status == "queued"
    # The requeued job is due now, so the same sweep rang for it
    assert str(job.id) in doorbell


def test_sweep_fails_stale_job_out_of_attempts(db, review_and_job):
    review, job = review_and_job
    job.attempts = job.max_attempts - 1
    db.flush()
    jobs.claim_job(db, job.id, "w1")  # attempts -> max
    job.heartbeat_at = _ago(jobs.STALE_AFTER_S + 60)
    db.flush()

    counts = jobs.sweep(db, None)
    db.refresh(job)
    db.refresh(review)
    assert counts["failed"] == 1
    assert job.status == "failed"
    assert review.status == "failed"
    assert review.error_user_message


def test_sweep_leaves_healthy_running_jobs_alone(db, review_and_job):
    _review, job = review_and_job
    jobs.claim_job(db, job.id, "w1")

    counts = jobs.sweep(db, None)
    db.refresh(job)
    assert job.status == "running"
    assert counts["requeued"] == counts["failed"] == 0


def test_sweep_cancels_flagged_queued_job(db, review_and_job):
    review, job = review_and_job
    job.cancel_requested = True
    db.flush()

    counts = jobs.sweep(db, None)
    db.refresh(job)
    db.refresh(review)
    assert counts["cancelled"] == 1
    assert job.status == "cancelled"
    assert review.status == "cancelled"


def test_sweep_rings_for_due_queued_jobs(db, review_and_job, doorbell):
    _review, job = review_and_job
    doorbell.clear()  # drop the ring from review creation

    counts = jobs.sweep(db, None)
    assert counts["rung"] == 1
    assert doorbell == [str(job.id)]

    job.run_after = datetime.now(UTC) + timedelta(hours=1)
    db.flush()
    doorbell.clear()
    counts = jobs.sweep(db, None)
    assert counts["rung"] == 0
    assert doorbell == []


# --- process_job ---


def test_process_job_completes_end_to_end(db, github, review_and_job):
    review, job = review_and_job
    wire_fixture(github, "python_buggy")
    expected = json.loads((FIXTURES / "python_buggy" / "expected.json").read_text())

    outcome = runner.process_job(db, job.id, "w1")

    assert outcome == "completed"
    db.refresh(job)
    db.refresh(review)
    assert job.status == "completed"
    assert job.finished_at is not None
    assert review.status == "completed"
    assert review.completed_at is not None
    assert review.findings_count == len(expected)
    assert review.summary
    assert review.pipeline_version

    rows = list(db.execute(select(Finding).where(Finding.review_id == review.id)).scalars())
    assert len(rows) == len(expected)
    assert {row.severity for row in rows} == {item["severity"] for item in expected}
    assert all(row.fingerprint for row in rows)
    by_severity: dict[str, int] = {}
    for item in expected:
        by_severity[item["severity"]] = by_severity.get(item["severity"], 0) + 1
    assert review.severity_counts == by_severity


def test_process_job_skips_unclaimable(db, github, review_and_job):
    _review, job = review_and_job
    jobs.claim_job(db, job.id, "other-worker")
    assert runner.process_job(db, job.id, "w1") == "not_claimed"


def test_process_job_retries_transient_github_failure(db, github, review_and_job):
    review, job = review_and_job
    github.responses[f"/repos/octocat/alpha/compare/{BASE_SHA}...{HEAD_SHA_41}"] = httpx.Response(
        500, json={"message": "boom"}
    )

    outcome = runner.process_job(db, job.id, "w1")

    assert outcome == "retried"
    db.refresh(job)
    db.refresh(review)
    assert job.status == "queued"
    assert job.attempts == 1
    assert job.run_after > datetime.now(UTC)
    assert review.status == "queued"


def test_process_job_fails_once_attempts_exhaust(db, github, review_and_job):
    review, job = review_and_job
    job.attempts = job.max_attempts - 1
    db.flush()
    github.responses[f"/repos/octocat/alpha/compare/{BASE_SHA}...{HEAD_SHA_41}"] = httpx.Response(
        500, json={"message": "boom"}
    )

    outcome = runner.process_job(db, job.id, "w1")

    assert outcome == "failed"
    db.refresh(job)
    db.refresh(review)
    assert job.status == "failed"
    assert review.status == "failed"
    assert review.error_user_message


def test_process_job_fails_fast_when_snapshot_is_gone(db, github, review_and_job):
    review, job = review_and_job
    # No compare entry wired: the fake answers 404, like a force-pushed-away SHA

    outcome = runner.process_job(db, job.id, "w1")

    assert outcome == "failed"
    db.refresh(job)
    db.refresh(review)
    assert job.status == "failed"
    assert job.attempts == 1  # no retry burned on a permanent failure
    assert review.status == "failed"


def test_process_job_fails_without_github_connection(db, github, review_and_job):
    review, job = review_and_job
    connection = db.execute(
        select(ProviderConnection).where(ProviderConnection.user_id == review.user_id)
    ).scalar_one()
    connection.token_invalid = True
    db.flush()

    outcome = runner.process_job(db, job.id, "w1")

    assert outcome == "failed"
    db.refresh(review)
    assert review.status == "failed"
    assert "reconnect" in review.error_user_message.lower()


def test_process_job_honors_cancellation_checkpoint(db, github, review_and_job):
    review, job = review_and_job
    # Raced flag: cancel_requested lands while the job is already claimed
    job.cancel_requested = True
    db.flush()

    outcome = runner.process_job(db, job.id, "w1")

    assert outcome == "cancelled"
    db.refresh(job)
    db.refresh(review)
    assert job.status == "cancelled"
    assert review.status == "cancelled"


def test_requeue_leaves_finished_jobs_alone(db, review_and_job):
    # Zombie scenario: worker A claimed, went stale, the job was reclaimed and
    # completed by B. A's late retry must not strand the review in 'queued'.
    review, job = review_and_job
    jobs.claim_job(db, job.id, "w1")
    job.status = "completed"
    review.status = "completed"
    db.commit()

    assert jobs.requeue_for_retry(db, job, "w1", "boom", "detail") is False
    db.refresh(job)
    db.refresh(review)
    assert job.status == "completed"
    assert review.status == "completed"


def test_fail_job_requires_ownership(db, review_and_job):
    review, job = review_and_job
    jobs.claim_job(db, job.id, "w1")
    job.locked_by = "w2"  # the sweep reclaimed it and w2 now runs it
    db.commit()

    assert jobs.fail_job(db, job, "w1", "boom", "detail") is False
    db.refresh(job)
    db.refresh(review)
    assert job.status == "running"
    assert review.status == "running"


def test_cancel_job_requires_ownership(db, review_and_job):
    review, job = review_and_job
    jobs.claim_job(db, job.id, "w1")
    job.locked_by = "w2"
    db.commit()

    assert jobs.cancel_job(db, job, "w1") is False
    db.refresh(job)
    assert job.status == "running"


def test_next_due_job_id_returns_only_due_jobs(db, review_and_job):
    _review, job = review_and_job
    assert jobs.next_due_job_id(db) == job.id

    job.run_after = datetime.now(UTC) + timedelta(hours=1)
    db.commit()
    assert jobs.next_due_job_id(db) is None


def test_sweep_counts_only_delivered_rings(db, review_and_job, monkeypatch):
    _review, _job = review_and_job
    monkeypatch.setattr(queue, "notify", lambda client, job_id: False)

    counts = jobs.sweep(db, None)
    assert counts["rung"] == 0


def test_process_job_reports_lost_when_reclaimed_mid_run(db, github, review_and_job, monkeypatch):
    from sqlalchemy import update

    review, job = review_and_job
    wire_fixture(github, "python_buggy")

    def steal_ownership(client, full_name, head_sha, files, workspace):
        # Simulates the sweep reclaiming the job while this worker is deep in
        # the no-DB analysis phase
        db.execute(update(ReviewJob).where(ReviewJob.id == job.id).values(locked_by="w2"))
        db.commit()

    monkeypatch.setattr(runner, "populate_workspace", steal_ownership)

    outcome = runner.process_job(db, job.id, "w1")

    assert outcome == "lost"
    db.refresh(job)
    assert job.locked_by == "w2"
    assert job.status == "running"  # w2's run is untouched
    assert list(db.execute(select(Finding)).scalars()) == []


def test_process_job_fails_oversized_pr(db, github, review_and_job):
    review, job = review_and_job
    files = [
        {"filename": f"src/file_{i}.py", "status": "added", "patch": "@@ -0,0 +1 @@\n+x = 1"}
        for i in range(300)
    ]
    github.compares[("octocat/alpha", BASE_SHA, HEAD_SHA_41)] = {"status": "ahead", "files": files}

    outcome = runner.process_job(db, job.id, "w1")

    assert outcome == "failed"
    db.refresh(review)
    assert review.status == "failed"
    assert "files" in review.error_user_message.lower()


def test_build_diff_text_reconstructs_renames(db):
    from app.analysis.diffs.parser import build_diff_index

    diff = runner.build_diff_text(
        [
            {
                "filename": "app/new.py",
                "previous_filename": "app/old.py",
                "status": "renamed",
                "patch": "@@ -1 +1 @@\n-x = 1\n+x = 2",
            }
        ]
    )
    index = build_diff_index(diff)
    assert index.files["app/new.py"].status == "renamed"
    assert index.files["app/new.py"].old_path == "app/old.py"


def test_patchless_files_are_skipped_everywhere(db, github, tmp_path):
    from app.services.github_client import GitHubClient

    entry = {"filename": "logo.png", "status": "modified"}  # binary: GitHub sends no patch
    assert runner.build_diff_text([entry]) == ""

    with GitHubClient("gho_test") as client:
        runner.populate_workspace(client, "octocat/alpha", HEAD_SHA_41, [entry], tmp_path)
    assert list(tmp_path.iterdir()) == []
    assert github.calls == []  # no content fetch was even attempted


def test_process_job_completes_empty_diff(db, github, review_and_job):
    review, job = review_and_job
    github.compares[("octocat/alpha", BASE_SHA, HEAD_SHA_41)] = {"status": "identical", "files": []}

    outcome = runner.process_job(db, job.id, "w1")

    assert outcome == "completed"
    db.refresh(review)
    assert review.status == "completed"
    assert review.findings_count == 0
    assert review.severity_counts == {}


def test_process_job_completes_clean_fixture_with_zero_findings(db, github, review_and_job):
    review, job = review_and_job
    wire_fixture(github, "clean")

    outcome = runner.process_job(db, job.id, "w1")

    assert outcome == "completed"
    db.refresh(review)
    assert review.status == "completed"
    assert review.findings_count == 0
    assert list(db.execute(select(Finding)).scalars()) == []


def test_process_job_records_the_ai_provider_in_pipeline_version(db, github, review_and_job):
    review, job = review_and_job
    wire_fixture(github, "python_buggy")

    outcome = runner.process_job(db, job.id, "w1")

    assert outcome == "completed"
    db.refresh(review)
    # Default config is the mock provider: full AI pipeline, zero cost.
    # The recorded mode must match how the review actually ran.
    assert review.pipeline_version.startswith("cheap ")
    assert "ai=mock" in review.pipeline_version
    assert "deterministic_only" not in review.pipeline_version


def test_process_job_retries_when_the_ai_provider_errors(db, github, review_and_job, monkeypatch):
    review, job = review_and_job
    wire_fixture(github, "python_buggy")

    class DownProvider:
        def review(self, request):
            raise RuntimeError("anthropic is down")

    monkeypatch.setattr(
        runner, "resolve_provider", lambda db_, user_id: ("cheap", DownProvider(), "server")
    )

    outcome = runner.process_job(db, job.id, "w1")

    assert outcome == "retried"
    db.refresh(job)
    assert job.status == "queued"
    assert job.attempts == 1
    assert "RuntimeError" in job.error_detail


def test_process_job_fails_permanently_on_anthropic_auth_error(
    db, github, review_and_job, monkeypatch
):
    import anthropic

    review, job = review_and_job
    wire_fixture(github, "python_buggy")

    class RevokedKeyProvider:
        def review(self, request):
            raise anthropic.AuthenticationError(
                "invalid x-api-key",
                response=httpx.Response(401, request=httpx.Request("POST", "https://x")),
                body=None,
            )

    monkeypatch.setattr(
        runner, "resolve_provider", lambda db_, user_id: ("cheap", RevokedKeyProvider(), "server")
    )

    outcome = runner.process_job(db, job.id, "w1")

    assert outcome == "failed"
    db.refresh(job)
    db.refresh(review)
    assert job.status == "failed"
    assert job.attempts == 1  # a revoked key burns no retries
    assert "misconfigured" in review.error_user_message.lower()


def test_process_job_uses_the_review_authors_own_key(db, github, review_and_job, monkeypatch):
    from app import security
    from app.ai.gemini_provider import GeminiProvider
    from app.analysis.ai_review import AIResponse
    from app.models import UserAIKey

    review, job = review_and_job
    wire_fixture(github, "python_buggy")
    db.add(
        UserAIKey(
            user_id=review.user_id,
            provider="gemini",
            key_enc=security.encrypt_token("AIzaFakeTestKey1234"),
        )
    )
    db.commit()

    def fake_review(self, request):
        assert self.model == "gemini-3.6-flash"  # BYOK default model resolved
        return AIResponse(raw_text='{"findings": []}', refused=False, model=self.model)

    monkeypatch.setattr(GeminiProvider, "review", fake_review)

    outcome = runner.process_job(db, job.id, "w1")

    assert outcome == "completed"
    db.refresh(review)
    assert review.pipeline_version.startswith("cheap ")
    assert "ai=gemini-3.6-flash" in review.pipeline_version


def test_process_job_tells_the_user_when_their_key_is_unreadable(db, github, review_and_job):
    from app.models import UserAIKey

    review, job = review_and_job
    wire_fixture(github, "python_buggy")
    # Stored under a rotated encryption key: only the user can fix this,
    # by re-saving the key in Settings
    db.add(UserAIKey(user_id=review.user_id, provider="gemini", key_enc="not-a-fernet-token"))
    db.commit()

    outcome = runner.process_job(db, job.id, "w1")

    assert outcome == "failed"
    db.refresh(job)
    db.refresh(review)
    assert job.attempts == 1
    assert "your ai api key" in review.error_user_message.lower()


def test_process_job_blames_the_users_key_when_it_fails(db, github, review_and_job, monkeypatch):
    from app import security
    from app.ai.errors import AIProviderConfigError
    from app.ai.gemini_provider import GeminiProvider
    from app.models import UserAIKey

    review, job = review_and_job
    wire_fixture(github, "python_buggy")
    db.add(
        UserAIKey(
            user_id=review.user_id,
            provider="gemini",
            key_enc=security.encrypt_token("AIzaRevokedKey5678"),
        )
    )
    db.commit()

    def failing_review(self, request):
        raise AIProviderConfigError("Gemini rejected the request (401)")

    monkeypatch.setattr(GeminiProvider, "review", failing_review)

    outcome = runner.process_job(db, job.id, "w1")

    assert outcome == "failed"
    db.refresh(job)
    db.refresh(review)
    assert job.attempts == 1  # no retries burned on a bad user key
    assert "your ai api key" in review.error_user_message.lower()


def test_process_job_persists_validated_ai_findings(db, github, review_and_job, monkeypatch):
    from app.ai.mock import MockProvider

    review, job = review_and_job
    wire_fixture(github, "python_buggy")
    provider = MockProvider(
        candidates=[
            {
                "file_path": "app/report.py",
                "start_line": 15,
                "end_line": 15,
                "severity": "medium",
                "category": "performance",
                "confidence": "medium",
                "title": "Query string rebuilt on every call",
                "explanation": None,
                "recommendation": None,
            },
            {  # hallucinated: must never reach the database
                "file_path": "app/invented.py",
                "start_line": 1,
                "end_line": 1,
                "severity": "high",
                "category": "security",
                "confidence": "high",
                "title": "Fabricated",
                "explanation": None,
                "recommendation": None,
            },
        ]
    )
    monkeypatch.setattr(
        runner, "resolve_provider", lambda db_, user_id: ("cheap", provider, "server")
    )

    outcome = runner.process_job(db, job.id, "w1")

    assert outcome == "completed"
    rows = list(db.execute(select(Finding)).scalars())
    ai_rows = [row for row in rows if row.source == "ai"]
    assert len(ai_rows) == 1
    assert ai_rows[0].title == "Query string rebuilt on every call"
    assert not any(row.file_path == "app/invented.py" for row in rows)


def test_process_job_fails_fast_on_ai_misconfiguration(db, github, review_and_job, monkeypatch):
    from app.config import settings

    review, job = review_and_job
    monkeypatch.setattr(settings, "ai_provider", "anthropic")
    monkeypatch.setattr(settings, "anthropic_api_key", "")

    outcome = runner.process_job(db, job.id, "w1")

    assert outcome == "failed"
    db.refresh(job)
    db.refresh(review)
    assert job.status == "failed"
    assert job.attempts == 1  # config errors burn no retries
    assert "ANTHROPIC_API_KEY" in job.error_detail


def test_process_job_refuses_workspace_escape(db, github, review_and_job):
    review, job = review_and_job
    payload = wire_fixture(github, "python_buggy")
    payload["files"].append(
        {
            "filename": "../../evil.py",
            "status": "added",
            "patch": "@@ -0,0 +1 @@\n+print('escaped')",
        }
    )

    outcome = runner.process_job(db, job.id, "w1")

    # The hostile path is dropped, the honest files still get reviewed
    assert outcome == "completed"
    fetched_paths = [request.url.path for request in github.calls]
    assert not any("evil" in path for path in fetched_paths)
