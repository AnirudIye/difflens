import uuid
from datetime import datetime
from typing import Any, Final

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# The partial unique indexes that carry the concurrency rules. Named here,
# beside the Index() calls that DECLARE them, because app code has to recognise
# them by name when Postgres refuses a write: a violation of one of these is a
# conflict to translate, not a server error. Nothing calls metadata.create_all,
# so alembic is the only thing that ever creates them and the migrations keep
# their own literals on purpose: a migration is a historical record and must
# not change when a constant does. That leaves these strings agreeing with
# the database by convention alone, which is what
# test_the_named_indexes_are_the_ones_the_migrations_built exists to check.
LIVE_REVIEW_INDEX: Final = "uq_reviews_pr_sha_live"
LIVE_REPO_REVIEW_INDEX: Final = "uq_reviews_repo_sha_live"
LIVE_JOB_INDEX: Final = "uq_jobs_one_live_per_review"


class Base(DeclarativeBase):
    type_annotation_map = {
        uuid.UUID: UUID(as_uuid=True),
        datetime: DateTime(timezone=True),
        str: Text,
        dict[str, Any]: JSONB,
    }


def uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(primary_key=True, server_default=text("gen_random_uuid()"))


def created_now() -> Mapped[datetime]:
    return mapped_column(server_default=func.now())


class User(Base):
    __tablename__ = "users"
    __table_args__ = (UniqueConstraint("github_id", name="uq_users_github_id"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    github_id: Mapped[int] = mapped_column(BigInteger)
    login: Mapped[str]
    name: Mapped[str | None]
    avatar_url: Mapped[str | None]
    created_at: Mapped[datetime] = created_now()
    updated_at: Mapped[datetime] = created_now()


class Session(Base):
    __tablename__ = "sessions"
    __table_args__ = (UniqueConstraint("token_hash", name="uq_sessions_token_hash"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    token_hash: Mapped[str]
    expires_at: Mapped[datetime] = mapped_column(index=True)
    created_at: Mapped[datetime] = created_now()
    last_seen_at: Mapped[datetime | None]


class ProviderConnection(Base):
    __tablename__ = "provider_connections"
    __table_args__ = (
        UniqueConstraint("user_id", "provider", name="uq_provider_connections_user_id_provider"),
        UniqueConstraint(
            "provider", "provider_account_id", name="uq_provider_connections_provider_account"
        ),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    provider: Mapped[str] = mapped_column(server_default=text("'github'"))
    provider_account_id: Mapped[int] = mapped_column(BigInteger)
    access_token_enc: Mapped[str]
    scopes: Mapped[str] = mapped_column(server_default=text("''"))
    token_invalid: Mapped[bool] = mapped_column(server_default=text("false"))
    created_at: Mapped[datetime] = created_now()
    updated_at: Mapped[datetime] = created_now()


class UserAIKey(Base):
    """One bring-your-own AI key per user, encrypted at rest like GitHub tokens.

    Reviews triggered by this user run on their key instead of the server's
    provider. The plaintext key is never stored or returned; only a hint.
    """

    __tablename__ = "user_ai_keys"
    __table_args__ = (
        UniqueConstraint("user_id", name="uq_user_ai_keys_user_id"),
        CheckConstraint(
            "provider IN ('anthropic', 'gemini', 'openai')",
            name="ck_user_ai_keys_provider",
        ),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    provider: Mapped[str]
    key_enc: Mapped[str]
    model: Mapped[str | None]
    created_at: Mapped[datetime] = created_now()
    updated_at: Mapped[datetime] = created_now()


class Repository(Base):
    __tablename__ = "repositories"
    __table_args__ = (
        UniqueConstraint("github_id", name="uq_repositories_github_id"),
        UniqueConstraint("full_name", name="uq_repositories_full_name"),
        # At most one demo repository: only is_demo rows are indexed and they
        # all hold the same value, so uniqueness caps the set at one
        Index(
            "uq_repositories_single_demo",
            "is_demo",
            unique=True,
            postgresql_where=text("is_demo"),
        ),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    github_id: Mapped[int] = mapped_column(BigInteger)
    owner: Mapped[str | None]
    name: Mapped[str | None]
    full_name: Mapped[str]
    private: Mapped[bool] = mapped_column(server_default=text("false"))
    default_branch: Mapped[str | None]
    html_url: Mapped[str | None]
    last_synced_at: Mapped[datetime | None]
    # The public demo's repository. Nothing about it is fetched from GitHub,
    # and the worker checks this before it looks for a token.
    is_demo: Mapped[bool] = mapped_column(server_default=text("false"))
    created_at: Mapped[datetime] = created_now()
    updated_at: Mapped[datetime] = created_now()


class UserRepository(Base):
    __tablename__ = "user_repositories"

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    repository_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("repositories.id", ondelete="CASCADE"), primary_key=True, index=True
    )
    created_at: Mapped[datetime] = created_now()


class PullRequest(Base):
    __tablename__ = "pull_requests"
    __table_args__ = (
        UniqueConstraint(
            "repository_id", "github_number", name="uq_pull_requests_repository_id_github_number"
        ),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    repository_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("repositories.id", ondelete="CASCADE")
    )
    github_number: Mapped[int]
    github_id: Mapped[int | None] = mapped_column(BigInteger)
    title: Mapped[str]
    author_login: Mapped[str | None]
    state: Mapped[str]
    base_ref: Mapped[str | None]
    head_ref: Mapped[str | None]
    base_sha: Mapped[str | None]
    head_sha: Mapped[str]
    html_url: Mapped[str | None]
    github_updated_at: Mapped[datetime | None]
    created_at: Mapped[datetime] = created_now()
    updated_at: Mapped[datetime] = created_now()


class Review(Base):
    __tablename__ = "reviews"
    __table_args__ = (
        CheckConstraint(
            "status IN ('queued', 'running', 'completed', 'failed', 'cancelled', 'superseded')",
            name="ck_reviews_status",
        ),
        # A review targets exactly one thing: a pull request or a repository
        # snapshot. Postgres treats NULLs as distinct in unique indexes, so a
        # review with a NULL pull_request_id would slip past the PR live index;
        # the repo live index below is what closes that gap.
        CheckConstraint(
            "(pull_request_id IS NULL) != (repository_id IS NULL)",
            name="ck_reviews_one_target",
        ),
        # Only a repository snapshot has no base commit
        CheckConstraint(
            "pull_request_id IS NULL OR base_sha IS NOT NULL",
            name="ck_reviews_pr_has_base",
        ),
        Index("ix_reviews_user_id_created_at", "user_id", text("created_at DESC")),
        Index("ix_reviews_pull_request_id_created_at", "pull_request_id", text("created_at DESC")),
        Index("ix_reviews_repository_id_created_at", "repository_id", text("created_at DESC")),
        # One live review per (PR, commit): reruns are allowed only after failure/cancellation
        Index(
            LIVE_REVIEW_INDEX,
            "pull_request_id",
            "head_sha",
            unique=True,
            postgresql_where=text("status IN ('queued', 'running', 'completed')"),
        ),
        # One live review per (repository, commit), the repo-snapshot sibling
        # of the index above and the structural cap on concurrent repo jobs
        Index(
            LIVE_REPO_REVIEW_INDEX,
            "repository_id",
            "head_sha",
            unique=True,
            postgresql_where=text(
                "repository_id IS NOT NULL AND status IN ('queued', 'running', 'completed')"
            ),
        ),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    pull_request_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("pull_requests.id", ondelete="CASCADE")
    )
    repository_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("repositories.id", ondelete="CASCADE")
    )
    head_sha: Mapped[str]
    base_sha: Mapped[str | None]
    status: Mapped[str] = mapped_column(server_default=text("'queued'"))
    summary: Mapped[str | None]
    findings_count: Mapped[int | None]
    severity_counts: Mapped[dict[str, Any] | None]
    pipeline_version: Mapped[str | None]
    error_user_message: Mapped[str | None]
    created_at: Mapped[datetime] = created_now()
    started_at: Mapped[datetime | None]
    completed_at: Mapped[datetime | None]


class ReviewJob(Base):
    __tablename__ = "review_jobs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('queued', 'running', 'completed', 'failed', 'cancelled')",
            name="ck_review_jobs_status",
        ),
        Index("ix_jobs_dequeue", "run_after", postgresql_where=text("status = 'queued'")),
        Index("ix_jobs_reclaim", "heartbeat_at", postgresql_where=text("status = 'running'")),
        Index(
            LIVE_JOB_INDEX,
            "review_id",
            unique=True,
            postgresql_where=text("status IN ('queued', 'running')"),
        ),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    review_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("reviews.id", ondelete="CASCADE"), index=True
    )
    status: Mapped[str] = mapped_column(server_default=text("'queued'"))
    attempts: Mapped[int] = mapped_column(server_default=text("0"))
    max_attempts: Mapped[int] = mapped_column(server_default=text("3"))
    run_after: Mapped[datetime] = mapped_column(server_default=func.now())
    cancel_requested: Mapped[bool] = mapped_column(server_default=text("false"))
    locked_by: Mapped[str | None]
    locked_at: Mapped[datetime | None]
    heartbeat_at: Mapped[datetime | None]
    error_user: Mapped[str | None]
    error_detail: Mapped[str | None]
    created_at: Mapped[datetime] = created_now()
    started_at: Mapped[datetime | None]
    finished_at: Mapped[datetime | None]


class Finding(Base):
    __tablename__ = "findings"
    __table_args__ = (
        CheckConstraint(
            "severity IN ('critical', 'high', 'medium', 'low', 'info')",
            name="ck_findings_severity",
        ),
        CheckConstraint(
            "category IN ('correctness', 'security', 'performance', "
            "'maintainability', 'testing', 'style')",
            name="ck_findings_category",
        ),
        CheckConstraint(
            "confidence IN ('high', 'medium', 'low')",
            name="ck_findings_confidence",
        ),
        CheckConstraint(
            "source IN ('deterministic', 'ai', 'hybrid')",
            name="ck_findings_source",
        ),
        CheckConstraint(
            "status IN ('open', 'dismissed', 'accepted')",
            name="ck_findings_status",
        ),
        Index("ix_findings_review_id_severity_file_path", "review_id", "severity", "file_path"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    review_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("reviews.id", ondelete="CASCADE"))
    file_path: Mapped[str]
    start_line: Mapped[int | None]
    end_line: Mapped[int | None]
    severity: Mapped[str]
    category: Mapped[str]
    confidence: Mapped[str | None]
    source: Mapped[str]
    fingerprint: Mapped[str]
    status: Mapped[str] = mapped_column(server_default=text("'open'"))
    title: Mapped[str]
    explanation: Mapped[str | None]
    recommendation: Mapped[str | None]
    created_at: Mapped[datetime] = created_now()


class Feedback(Base):
    __tablename__ = "feedback"
    __table_args__ = (
        CheckConstraint(
            "verdict IN ('useful', 'not_useful', 'dismissed')",
            name="ck_feedback_verdict",
        ),
        UniqueConstraint("finding_id", "user_id", name="uq_feedback_finding_id_user_id"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    finding_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("findings.id", ondelete="CASCADE"))
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    verdict: Mapped[str]
    note: Mapped[str | None]
    created_at: Mapped[datetime] = created_now()
    updated_at: Mapped[datetime] = created_now()
