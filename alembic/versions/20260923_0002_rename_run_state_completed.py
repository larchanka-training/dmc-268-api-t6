"""Rename the successful run state to completed.

Revision ID: 20260923_0002
Revises: 20260913_0001
Create Date: 2026-09-23
"""

from alembic import op

revision = "20260923_0002"
down_revision = "20260913_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TYPE run_state RENAME VALUE 'succeeded' TO 'completed'")


def downgrade() -> None:
    op.execute("ALTER TYPE run_state RENAME VALUE 'completed' TO 'succeeded'")
