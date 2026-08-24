"""Let a review target a repository snapshot as well as a pull request.

A repository review pins the default branch's head commit and reviews the
whole tree at that SHA, so it has a repository, a head_sha, and no base.
pull_request_id and base_sha therefore become nullable, a repository_id
column arrives, and two CHECKs keep the shape honest: every review targets
exactly one of a pull request or a repository, and only repository reviews
may omit a base commit.

The partial unique index is the part that matters. Postgres treats NULLs as
distinct in unique indexes, so once pull_request_id can be NULL the existing
uq_reviews_pr_sha_live no longer constrains repo reviews at all: it fails
open, permitting unlimited live reviews of one repository at one commit.
uq_reviews_repo_sha_live is the repo-shaped sibling that closes the gap, and
it is also the structural cap on concurrent repo jobs, the same property the
threat model leans on for the PR index.

Revision ID: 0006
Revises: 0005
Create Date: 2026-08-24

"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column("reviews", "pull_request_id", nullable=True)
    op.alter_column("reviews", "base_sha", nullable=True)
    op.add_column(
        "reviews",
        sa.Column(
            "repository_id",
            UUID(),
            sa.ForeignKey("repositories.id", ondelete="CASCADE"),
            nullable=True,
        ),
    )
    op.create_check_constraint(
        "ck_reviews_one_target",
        "reviews",
        "(pull_request_id IS NULL) != (repository_id IS NULL)",
    )
    op.create_check_constraint(
        "ck_reviews_pr_has_base",
        "reviews",
        "pull_request_id IS NULL OR base_sha IS NOT NULL",
    )
    op.create_index(
        "uq_reviews_repo_sha_live",
        "reviews",
        ["repository_id", "head_sha"],
        unique=True,
        postgresql_where=sa.text(
            "repository_id IS NOT NULL AND status IN ('queued', 'running', 'completed')"
        ),
    )
    op.create_index(
        "ix_reviews_repository_id_created_at",
        "reviews",
        ["repository_id", sa.text("created_at DESC")],
    )


def downgrade() -> None:
    # Repository reviews cannot survive a schema without repository_id, so
    # the downgrade deletes them rather than leaving rows that violate the
    # restored NOT NULLs. Their findings cascade with them.
    op.execute("DELETE FROM reviews WHERE repository_id IS NOT NULL")
    op.drop_index("ix_reviews_repository_id_created_at", table_name="reviews")
    op.drop_index("uq_reviews_repo_sha_live", table_name="reviews")
    op.drop_constraint("ck_reviews_pr_has_base", "reviews", type_="check")
    op.drop_constraint("ck_reviews_one_target", "reviews", type_="check")
    op.drop_column("reviews", "repository_id")
    op.alter_column("reviews", "base_sha", nullable=False)
    op.alter_column("reviews", "pull_request_id", nullable=False)
