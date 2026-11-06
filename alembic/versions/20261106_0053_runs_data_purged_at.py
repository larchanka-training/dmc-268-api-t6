"""Mark the runs whose stale data the retention task has cleared (#457).

Revision ID: 20261106_0053
Revises: 20261027_0052
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "20261106_0053"
down_revision = "20261027_0052"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("runs", sa.Column("data_purged_at", sa.DateTime(timezone=True)))
    op.create_index(
        "ix_runs_data_retention_candidates",
        "runs",
        ["finished_at", "id"],
        postgresql_where=sa.text("data_purged_at IS NULL AND finished_at IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_runs_data_retention_candidates", table_name="runs")
    op.drop_column("runs", "data_purged_at")
