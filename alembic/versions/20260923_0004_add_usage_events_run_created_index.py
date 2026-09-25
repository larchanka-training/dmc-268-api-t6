"""Index usage events by run and creation time.

Revision ID: 20260923_0004
Revises: 20260923_0003
Create Date: 2026-09-23
"""

from alembic import op

revision = "20260923_0004"
down_revision = "20260923_0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index("ix_usage_events_run_created", "usage_events", ["run_id", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_usage_events_run_created", table_name="usage_events")
