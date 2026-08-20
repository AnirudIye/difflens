"""Feedback endpoints: upsert per (finding, user), ownership via the review.

The unique constraint carries the concurrency rule; these tests exercise it
through the API and check that GET /reviews/{id} reflects only the caller's
own verdicts.
"""

import uuid

import pytest
from sqlalchemy import select

from app.models import Feedback, Finding, PullRequest, Repository, Review, ReviewJob

HEAD = "a" * 40
BASE = "b" * 40


def _seed_review_with_finding(db, user, *, sha_prefix="a"):
    repo = Repository(github_id=int(uuid.uuid4().int % 10**8), full_name=f"seed/{user.login}")
    db.add(repo)
    db.flush()
    pull = PullRequest(
        repository_id=repo.id,
        github_number=7,
        title="Add the demo feature",
        state="open",
        head_sha=sha_prefix * 40,
    )
    db.add(pull)
    db.flush()
    review = Review(
        user_id=user.id,
        pull_request_id=pull.id,
        head_sha=pull.head_sha,
        base_sha=BASE,
        status="completed",
    )
    db.add(review)
    db.flush()
    finding = Finding(
        review_id=review.id,
        file_path="app/main.py",
        start_line=12,
        end_line=14,
        severity="high",
        category="correctness",
        source="ai",
        fingerprint=f"fp-{uuid.uuid4().hex[:8]}",
        title="Off by one in pagination",
    )
    db.add(finding)
    db.flush()
    return review, finding


def test_put_requires_auth(client):
    response = client.put(f"/findings/{uuid.uuid4()}/feedback", json={"verdict": "useful"})
    assert response.status_code == 401


def test_put_creates_and_shows_in_review(client, db, make_user_with_session):
    user, _ = make_user_with_session("alice")
    review, finding = _seed_review_with_finding(db, user)

    response = client.put(f"/findings/{finding.id}/feedback", json={"verdict": "useful"})
    assert response.status_code == 200
    assert response.json() == {"verdict": "useful"}

    body = client.get(f"/reviews/{review.id}").json()
    assert [item["feedback"] for item in body["findings"]] == ["useful"]


def test_put_upserts_to_one_row(client, db, make_user_with_session):
    user, _ = make_user_with_session("alice")
    _review, finding = _seed_review_with_finding(db, user)

    client.put(f"/findings/{finding.id}/feedback", json={"verdict": "useful"})
    response = client.put(f"/findings/{finding.id}/feedback", json={"verdict": "not_useful"})
    assert response.status_code == 200

    rows = db.execute(select(Feedback).where(Feedback.finding_id == finding.id)).scalars().all()
    assert len(rows) == 1
    assert rows[0].verdict == "not_useful"


@pytest.mark.parametrize("verdict", ["helpful", "", "USEFUL"])
def test_put_rejects_unknown_verdicts(client, db, make_user_with_session, verdict):
    user, _ = make_user_with_session("alice")
    _review, finding = _seed_review_with_finding(db, user)
    response = client.put(f"/findings/{finding.id}/feedback", json={"verdict": verdict})
    assert response.status_code == 422


def test_foreign_finding_looks_missing(client, db, make_user_with_session):
    alice, _ = make_user_with_session("alice")
    _review, finding = _seed_review_with_finding(db, alice)
    _bob, bob_token = make_user_with_session("bob")
    client.cookies.set("session", bob_token)

    put = client.put(f"/findings/{finding.id}/feedback", json={"verdict": "useful"})
    assert put.status_code == 404
    delete = client.delete(f"/findings/{finding.id}/feedback")
    assert delete.status_code == 404
    assert db.execute(select(Feedback)).scalars().all() == []


def test_missing_finding_404s(client, make_user_with_session):
    make_user_with_session("alice")
    response = client.put(f"/findings/{uuid.uuid4()}/feedback", json={"verdict": "useful"})
    assert response.status_code == 404


def test_delete_clears_and_is_idempotent(client, db, make_user_with_session):
    user, _ = make_user_with_session("alice")
    review, finding = _seed_review_with_finding(db, user)
    client.put(f"/findings/{finding.id}/feedback", json={"verdict": "dismissed"})

    first = client.delete(f"/findings/{finding.id}/feedback")
    assert first.status_code == 200
    assert first.json() == {"verdict": None}
    second = client.delete(f"/findings/{finding.id}/feedback")
    assert second.status_code == 200

    body = client.get(f"/reviews/{review.id}").json()
    assert [item["feedback"] for item in body["findings"]] == [None]


def test_verdicts_are_per_user(client, db, make_user_with_session):
    alice, alice_token = make_user_with_session("alice")
    review, finding = _seed_review_with_finding(db, alice)
    client.put(f"/findings/{finding.id}/feedback", json={"verdict": "useful"})

    # Bob cannot see Alice's review at all; Alice's verdict survives him trying
    _bob, bob_token = make_user_with_session("bob")
    client.cookies.set("session", bob_token)
    assert client.get(f"/reviews/{review.id}").status_code == 404

    client.cookies.set("session", alice_token)
    body = client.get(f"/reviews/{review.id}").json()
    assert [item["feedback"] for item in body["findings"]] == ["useful"]


def test_review_payload_carries_pull_context(client, db, make_user_with_session):
    user, _ = make_user_with_session("alice")
    review, _finding = _seed_review_with_finding(db, user)
    body = client.get(f"/reviews/{review.id}").json()
    pull = body["pull_request"]
    assert pull["number"] == 7
    assert pull["title"] == "Add the demo feature"
    assert pull["html_url"] is None  # nullable: the web type must allow it
    assert pull["repository_full_name"] == f"seed/{user.login}"
    assert uuid.UUID(pull["repository_id"])


@pytest.mark.parametrize(
    ("pipeline_version", "expected"),
    [
        ("cheap ai=gemini-3.6-flash ruff=0.14.0", "gemini-3.6-flash"),
        ("cheap ai=mock ruff=0.14.0", "mock"),  # the UI must see through the stub
        ("deterministic_only ruff=0.14.0 detect-secrets=1.5.0", None),
        ("cheap ai=mock ruff=0.14.0 ai_refused", "mock"),
        (None, None),  # never ran
    ],
)
def test_review_payload_reports_the_ai_model(
    client, db, make_user_with_session, pipeline_version, expected
):
    user, _ = make_user_with_session("alice")
    review, _finding = _seed_review_with_finding(db, user)
    review.pipeline_version = pipeline_version
    db.flush()

    body = client.get(f"/reviews/{review.id}").json()

    assert body["ai_model"] == expected


def test_cancel_payload_carries_feedback_and_pull_context(client, db, make_user_with_session):
    """The cancel response is applied verbatim by the review page; losing
    the pull context or verdicts there would blank the header silently."""
    user, _ = make_user_with_session("alice")
    review, finding = _seed_review_with_finding(db, user)
    review.status = "running"
    db.add(ReviewJob(review_id=review.id, status="running", locked_by="w1"))
    db.flush()
    put = client.put(f"/findings/{finding.id}/feedback", json={"verdict": "useful"})
    assert put.status_code == 200

    body = client.post(f"/reviews/{review.id}/cancel").json()

    assert body["status"] == "running"  # a running job is flagged, not finalized
    assert body["cancel_requested"] is True
    assert [item["feedback"] for item in body["findings"]] == ["useful"]
    assert body["pull_request"]["number"] == 7
    assert body["pull_request"]["repository_full_name"] == f"seed/{user.login}"
