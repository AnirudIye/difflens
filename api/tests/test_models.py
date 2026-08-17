import pytest
from sqlalchemy import func, inspect, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError

from app.models import Feedback, Finding, PullRequest, Repository, Review, User

EXPECTED_TABLES = {
    "users",
    "sessions",
    "provider_connections",
    "repositories",
    "user_repositories",
    "pull_requests",
    "reviews",
    "review_jobs",
    "findings",
    "feedback",
}


def test_migrations_create_all_ten_tables(engine):
    assert EXPECTED_TABLES <= set(inspect(engine).get_table_names())


def _seed_pull_request(db):
    user = User(github_id=555001, login="reviewer")
    repo = Repository(github_id=555002, full_name="difflens/demo")
    db.add_all([user, repo])
    db.flush()
    pull_request = PullRequest(
        repository_id=repo.id,
        github_number=7,
        title="Add the demo feature",
        state="open",
        head_sha="a" * 40,
    )
    db.add(pull_request)
    db.flush()
    return user, pull_request


def test_one_live_review_per_pr_and_sha(db):
    user, pull_request = _seed_pull_request(db)

    def queued_review() -> Review:
        return Review(
            user_id=user.id,
            pull_request_id=pull_request.id,
            head_sha=pull_request.head_sha,
            base_sha="b" * 40,
            status="queued",
        )

    first = queued_review()
    db.add(first)
    # Commit so the rollback after the expected violation only discards the duplicate
    db.commit()

    db.add(queued_review())
    with pytest.raises(IntegrityError) as excinfo:
        db.flush()
    assert "uq_reviews_pr_sha_live" in str(excinfo.value)
    db.rollback()

    first.status = "failed"
    db.commit()

    db.add(queued_review())
    db.flush()
    assert db.scalar(select(func.count()).select_from(Review)) == 2


def test_feedback_upserts_per_finding_and_user(db):
    user, pull_request = _seed_pull_request(db)
    review = Review(
        user_id=user.id,
        pull_request_id=pull_request.id,
        head_sha=pull_request.head_sha,
        base_sha="b" * 40,
    )
    db.add(review)
    db.flush()
    finding = Finding(
        review_id=review.id,
        file_path="app/main.py",
        severity="high",
        category="correctness",
        source="ai",
        fingerprint="fp-demo-1",
        title="Off by one in pagination",
    )
    db.add(finding)
    db.flush()

    def upsert(verdict: str) -> None:
        stmt = pg_insert(Feedback).values(finding_id=finding.id, user_id=user.id, verdict=verdict)
        stmt = stmt.on_conflict_do_update(
            constraint="uq_feedback_finding_id_user_id",
            set_={"verdict": stmt.excluded.verdict, "updated_at": func.now()},
        )
        db.execute(stmt)

    upsert("useful")
    upsert("not_useful")

    rows = db.execute(select(Feedback).where(Feedback.finding_id == finding.id)).scalars().all()
    assert len(rows) == 1
    assert rows[0].verdict == "not_useful"
