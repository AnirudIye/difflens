"""Contact messages: the delivery path behind the /contact form.

The table is the source of truth. Forwarding by email is best effort and
recorded in `forwarded`, so a message whose email never went out still sits
here, readable in the database console, and nothing a sender wrote is lost
to a mail outage or an unset API key.

Deliberately absent: the sender's IP address. Anyone may write here without
an account, including someone sending a privacy rights request, so the row
holds only what the sender chose to type. Rate limiting is per-IP but lives
in Redis, where it expires with the window.

Revision ID: 0007
Revises: 0006

"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "contact_messages",
        sa.Column("id", UUID(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("name", sa.Text(), nullable=True),
        sa.Column("email", sa.Text(), nullable=True),
        sa.Column("subject", sa.Text(), nullable=True),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("forwarded", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )


def downgrade() -> None:
    op.drop_table("contact_messages")
