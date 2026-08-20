"""Mark the one repository the public demo reviews.

The demo needs a way to say "this row is the demo" that the worker can check
before it looks for a GitHub token, and that the public demo endpoints can
scope every query to. A boolean on repositories carries it, and everything
else derives: the demo pull request is the one whose repository is marked,
the demo reviews are the ones on that pull request.

The partial unique index is the part that matters. Only rows with is_demo
true are indexed, and they all hold the same value, so the index permits
exactly one demo repository. Without it, a second seed run against a
half-seeded database would leave two demo repositories and the public
endpoints would have to guess which one they meant.

Revision ID: 0005
Revises: 0004
Create Date: 2026-08-20

"""

import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "repositories",
        sa.Column("is_demo", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )
    op.create_index(
        "uq_repositories_single_demo",
        "repositories",
        ["is_demo"],
        unique=True,
        postgresql_where=sa.text("is_demo"),
    )


def downgrade() -> None:
    op.drop_index("uq_repositories_single_demo", table_name="repositories")
    op.drop_column("repositories", "is_demo")
