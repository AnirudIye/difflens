"""Initial schema: all ten DiffLens tables.

Revision ID: 0001
Revises:
Create Date: 2026-08-17

"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

REVIEW_STATUSES = "('queued', 'running', 'completed', 'failed', 'cancelled')"


def _uuid_pk() -> sa.Column:
    return sa.Column("id", UUID(), primary_key=True, server_default=sa.text("gen_random_uuid()"))


def _timestamptz(name: str, **kwargs) -> sa.Column:
    return sa.Column(name, sa.DateTime(timezone=True), **kwargs)


def _created_at() -> sa.Column:
    return _timestamptz("created_at", nullable=False, server_default=sa.func.now())


def _updated_at() -> sa.Column:
    return _timestamptz("updated_at", nullable=False, server_default=sa.func.now())


def upgrade() -> None:
    op.create_table(
        "users",
        _uuid_pk(),
        sa.Column("github_id", sa.BigInteger(), nullable=False),
        sa.Column("login", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=True),
        sa.Column("avatar_url", sa.Text(), nullable=True),
        _created_at(),
        _updated_at(),
        sa.UniqueConstraint("github_id", name="uq_users_github_id"),
    )

    op.create_table(
        "sessions",
        _uuid_pk(),
        sa.Column("user_id", UUID(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("token_hash", sa.Text(), nullable=False),
        _timestamptz("expires_at", nullable=False),
        _created_at(),
        _timestamptz("last_seen_at", nullable=True),
        sa.UniqueConstraint("token_hash", name="uq_sessions_token_hash"),
    )
    op.create_index("ix_sessions_expires_at", "sessions", ["expires_at"])

    op.create_table(
        "provider_connections",
        _uuid_pk(),
        sa.Column("user_id", UUID(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("provider", sa.Text(), nullable=False, server_default=sa.text("'github'")),
        sa.Column("provider_account_id", sa.BigInteger(), nullable=False),
        sa.Column("access_token_enc", sa.Text(), nullable=False),
        sa.Column("scopes", sa.Text(), nullable=False, server_default=sa.text("''")),
        sa.Column("token_invalid", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        _created_at(),
        _updated_at(),
        sa.UniqueConstraint("user_id", "provider", name="uq_provider_connections_user_id_provider"),
        sa.UniqueConstraint(
            "provider", "provider_account_id", name="uq_provider_connections_provider_account"
        ),
    )

    op.create_table(
        "repositories",
        _uuid_pk(),
        sa.Column("github_id", sa.BigInteger(), nullable=False),
        sa.Column("owner", sa.Text(), nullable=True),
        sa.Column("name", sa.Text(), nullable=True),
        sa.Column("full_name", sa.Text(), nullable=False),
        sa.Column("private", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("default_branch", sa.Text(), nullable=True),
        sa.Column("html_url", sa.Text(), nullable=True),
        _timestamptz("last_synced_at", nullable=True),
        _created_at(),
        _updated_at(),
        sa.UniqueConstraint("github_id", name="uq_repositories_github_id"),
        sa.UniqueConstraint("full_name", name="uq_repositories_full_name"),
    )

    op.create_table(
        "user_repositories",
        sa.Column(
            "user_id", UUID(), sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
        ),
        sa.Column(
            "repository_id",
            UUID(),
            sa.ForeignKey("repositories.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        _created_at(),
    )
    op.create_index("ix_user_repositories_repository_id", "user_repositories", ["repository_id"])

    op.create_table(
        "pull_requests",
        _uuid_pk(),
        sa.Column(
            "repository_id",
            UUID(),
            sa.ForeignKey("repositories.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("github_number", sa.Integer(), nullable=False),
        sa.Column("github_id", sa.BigInteger(), nullable=True),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("author_login", sa.Text(), nullable=True),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("base_ref", sa.Text(), nullable=True),
        sa.Column("head_ref", sa.Text(), nullable=True),
        sa.Column("base_sha", sa.Text(), nullable=True),
        sa.Column("head_sha", sa.Text(), nullable=False),
        sa.Column("html_url", sa.Text(), nullable=True),
        _timestamptz("github_updated_at", nullable=True),
        _created_at(),
        _updated_at(),
        sa.UniqueConstraint(
            "repository_id", "github_number", name="uq_pull_requests_repository_id_github_number"
        ),
    )

    op.create_table(
        "reviews",
        _uuid_pk(),
        sa.Column("user_id", UUID(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column(
            "pull_request_id",
            UUID(),
            sa.ForeignKey("pull_requests.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("head_sha", sa.Text(), nullable=False),
        sa.Column("base_sha", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default=sa.text("'queued'")),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("findings_count", sa.Integer(), nullable=True),
        sa.Column("severity_counts", JSONB(), nullable=True),
        sa.Column("pipeline_version", sa.Text(), nullable=True),
        sa.Column("error_user_message", sa.Text(), nullable=True),
        _created_at(),
        _timestamptz("started_at", nullable=True),
        _timestamptz("completed_at", nullable=True),
        sa.CheckConstraint(f"status IN {REVIEW_STATUSES}", name="ck_reviews_status"),
    )
    op.create_index(
        "ix_reviews_user_id_created_at", "reviews", ["user_id", sa.text("created_at DESC")]
    )
    op.create_index(
        "ix_reviews_pull_request_id_created_at",
        "reviews",
        ["pull_request_id", sa.text("created_at DESC")],
    )
    # One live review per (PR, commit): reruns are allowed only after failure/cancellation
    op.create_index(
        "uq_reviews_pr_sha_live",
        "reviews",
        ["pull_request_id", "head_sha"],
        unique=True,
        postgresql_where=sa.text("status IN ('queued', 'running', 'completed')"),
    )

    op.create_table(
        "review_jobs",
        _uuid_pk(),
        sa.Column(
            "review_id", UUID(), sa.ForeignKey("reviews.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("status", sa.Text(), nullable=False, server_default=sa.text("'queued'")),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("max_attempts", sa.Integer(), nullable=False, server_default=sa.text("3")),
        _timestamptz("run_after", nullable=False, server_default=sa.func.now()),
        sa.Column(
            "cancel_requested", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
        sa.Column("locked_by", sa.Text(), nullable=True),
        _timestamptz("locked_at", nullable=True),
        _timestamptz("heartbeat_at", nullable=True),
        sa.Column("error_user", sa.Text(), nullable=True),
        sa.Column("error_detail", sa.Text(), nullable=True),
        _created_at(),
        _timestamptz("started_at", nullable=True),
        _timestamptz("finished_at", nullable=True),
        sa.CheckConstraint(f"status IN {REVIEW_STATUSES}", name="ck_review_jobs_status"),
    )
    op.create_index("ix_review_jobs_review_id", "review_jobs", ["review_id"])
    op.create_index(
        "ix_jobs_dequeue",
        "review_jobs",
        ["run_after"],
        postgresql_where=sa.text("status = 'queued'"),
    )
    op.create_index(
        "ix_jobs_reclaim",
        "review_jobs",
        ["heartbeat_at"],
        postgresql_where=sa.text("status = 'running'"),
    )
    op.create_index(
        "uq_jobs_one_live_per_review",
        "review_jobs",
        ["review_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('queued', 'running')"),
    )

    op.create_table(
        "findings",
        _uuid_pk(),
        sa.Column(
            "review_id", UUID(), sa.ForeignKey("reviews.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("file_path", sa.Text(), nullable=False),
        sa.Column("start_line", sa.Integer(), nullable=True),
        sa.Column("end_line", sa.Integer(), nullable=True),
        sa.Column("severity", sa.Text(), nullable=False),
        sa.Column("category", sa.Text(), nullable=False),
        sa.Column("confidence", sa.Text(), nullable=True),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("fingerprint", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default=sa.text("'open'")),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("explanation", sa.Text(), nullable=True),
        sa.Column("recommendation", sa.Text(), nullable=True),
        _created_at(),
        sa.CheckConstraint(
            "severity IN ('critical', 'high', 'medium', 'low', 'info')",
            name="ck_findings_severity",
        ),
        sa.CheckConstraint(
            "category IN ('correctness', 'security', 'performance', "
            "'maintainability', 'testing', 'style')",
            name="ck_findings_category",
        ),
        sa.CheckConstraint(
            "confidence IN ('high', 'medium', 'low')", name="ck_findings_confidence"
        ),
        sa.CheckConstraint(
            "source IN ('deterministic', 'ai', 'hybrid')", name="ck_findings_source"
        ),
        sa.CheckConstraint(
            "status IN ('open', 'dismissed', 'accepted')", name="ck_findings_status"
        ),
    )
    op.create_index(
        "ix_findings_review_id_severity_file_path",
        "findings",
        ["review_id", "severity", "file_path"],
    )

    op.create_table(
        "feedback",
        _uuid_pk(),
        sa.Column(
            "finding_id", UUID(), sa.ForeignKey("findings.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("user_id", UUID(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("verdict", sa.Text(), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        _created_at(),
        _updated_at(),
        sa.CheckConstraint(
            "verdict IN ('useful', 'not_useful', 'dismissed')", name="ck_feedback_verdict"
        ),
        sa.UniqueConstraint("finding_id", "user_id", name="uq_feedback_finding_id_user_id"),
    )
    op.create_index("ix_feedback_user_id", "feedback", ["user_id"])


def downgrade() -> None:
    op.drop_table("feedback")
    op.drop_table("findings")
    op.drop_table("review_jobs")
    op.drop_table("reviews")
    op.drop_table("pull_requests")
    op.drop_table("user_repositories")
    op.drop_table("repositories")
    op.drop_table("provider_connections")
    op.drop_table("sessions")
    op.drop_table("users")
