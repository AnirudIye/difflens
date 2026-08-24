"""Scope the live-review indexes to the user who started the review.

Both live indexes keyed only on the target: one live review per (pull
request, commit) and one per (repository, commit), for the whole
installation. On a product whose whole subject is public repositories, two
people reviewing the same commit is ordinary, and the consequence was not a
saved duplicate but a hard block: a completed review counts as live, only
its owner can supersede it, and a foreign review's id is deliberately
withheld. The second user got a 409 they could not act on, about findings
they are not allowed to read, and it never cleared.

Adding user_id makes each account's live review its own. The property the
threat model leans on survives, because it was never about global
uniqueness: at most one live review per user per target per commit still
bounds anonymous work (every demo review belongs to the one demo user) and
still makes a double click idempotent.

Revision ID: 0008
Revises: 0007
Create Date: 2026-08-24

"""

import sqlalchemy as sa
from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None

LIVE = "status IN ('queued', 'running', 'completed')"


def upgrade() -> None:
    op.drop_index("uq_reviews_pr_sha_live", table_name="reviews")
    op.drop_index("uq_reviews_repo_sha_live", table_name="reviews")
    op.create_index(
        "uq_reviews_pr_sha_live",
        "reviews",
        ["user_id", "pull_request_id", "head_sha"],
        unique=True,
        postgresql_where=sa.text(LIVE),
    )
    op.create_index(
        "uq_reviews_repo_sha_live",
        "reviews",
        ["user_id", "repository_id", "head_sha"],
        unique=True,
        postgresql_where=sa.text(f"repository_id IS NOT NULL AND {LIVE}"),
    )


def downgrade() -> None:
    # Going back means one live review per target for everyone again, so any
    # second user's review of the same snapshot has to go first.
    op.execute(
        """
        DELETE FROM reviews r
        USING reviews keep
        WHERE r.status IN ('queued', 'running', 'completed')
          AND keep.status IN ('queued', 'running', 'completed')
          AND r.head_sha = keep.head_sha
          AND r.id <> keep.id
          AND r.created_at > keep.created_at
          AND (
            (r.pull_request_id IS NOT NULL AND r.pull_request_id = keep.pull_request_id)
            OR (r.repository_id IS NOT NULL AND r.repository_id = keep.repository_id)
          )
        """
    )
    op.drop_index("uq_reviews_repo_sha_live", table_name="reviews")
    op.drop_index("uq_reviews_pr_sha_live", table_name="reviews")
    op.create_index(
        "uq_reviews_pr_sha_live",
        "reviews",
        ["pull_request_id", "head_sha"],
        unique=True,
        postgresql_where=sa.text(LIVE),
    )
    op.create_index(
        "uq_reviews_repo_sha_live",
        "reviews",
        ["repository_id", "head_sha"],
        unique=True,
        postgresql_where=sa.text(f"repository_id IS NOT NULL AND {LIVE}"),
    )
