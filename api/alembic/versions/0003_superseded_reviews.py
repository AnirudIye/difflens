"""Re-running a commit: the old review becomes superseded, not a duplicate.

The partial unique index uq_reviews_pr_sha_live covers queued, running, and
completed, so a finished review blocks a second one at the same commit. That
invariant is worth keeping (it is what makes a double click harmless), but a
user who changes their AI key has a real reason to review the same commit
again. Superseded sits outside the index, so the old row stops blocking while
staying readable.

Revision ID: 0003
Revises: 0002
Create Date: 2026-08-20

"""

from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

OLD = "status IN ('queued', 'running', 'completed', 'failed', 'cancelled')"
NEW = "status IN ('queued', 'running', 'completed', 'failed', 'cancelled', 'superseded')"


def upgrade() -> None:
    op.drop_constraint("ck_reviews_status", "reviews", type_="check")
    op.create_check_constraint("ck_reviews_status", "reviews", NEW)


def downgrade() -> None:
    # Any superseded rows would violate the narrower constraint; they are
    # finished reviews, so failed is the closest terminal state that fits
    op.execute("UPDATE reviews SET status = 'failed' WHERE status = 'superseded'")
    op.drop_constraint("ck_reviews_status", "reviews", type_="check")
    op.create_check_constraint("ck_reviews_status", "reviews", OLD)
