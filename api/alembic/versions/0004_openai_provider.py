"""Allow OpenAI as a stored per-user AI provider.

The provider column is constrained rather than free text so a typo cannot
quietly produce a key nothing can use. Adding a provider therefore means
widening the constraint.

Revision ID: 0004
Revises: 0003
Create Date: 2026-08-20

"""

from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None

OLD = "provider IN ('anthropic', 'gemini')"
NEW = "provider IN ('anthropic', 'gemini', 'openai')"


def upgrade() -> None:
    op.drop_constraint("ck_user_ai_keys_provider", "user_ai_keys", type_="check")
    op.create_check_constraint("ck_user_ai_keys_provider", "user_ai_keys", NEW)


def downgrade() -> None:
    # Stored OpenAI keys would violate the narrower constraint. Dropping the
    # rows loses only an encrypted key the user can paste again, whereas
    # rewriting the provider would silently point it at the wrong vendor.
    op.execute("DELETE FROM user_ai_keys WHERE provider = 'openai'")
    op.drop_constraint("ck_user_ai_keys_provider", "user_ai_keys", type_="check")
    op.create_check_constraint("ck_user_ai_keys_provider", "user_ai_keys", OLD)
