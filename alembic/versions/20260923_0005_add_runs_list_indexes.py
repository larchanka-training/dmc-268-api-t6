"""Add indexes for paginated run lists.

Revision ID: 20260923_0005
Revises: 20260923_0004
Create Date: 2026-09-23
"""

from alembic import op

revision = "20260923_0005"
down_revision = "20260923_0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index("ix_runs_created_id", "runs", ["created_at", "id"])
    op.create_index("ix_runs_state_created_id", "runs", ["state", "created_at", "id"])
    op.create_index(
        "ix_runs_code_change_created_id", "runs", ["code_change_id", "created_at", "id"]
    )


def downgrade() -> None:
    op.drop_index("ix_runs_code_change_created_id", table_name="runs")
    op.drop_index("ix_runs_state_created_id", table_name="runs")
    op.drop_index("ix_runs_created_id", table_name="runs")
