"""Mark the runs the retention task has cleared and index the blobs it clears (#457).

Revision ID: 20261106_0059
Revises: 20261104_0058
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "20261106_0059"
down_revision = "20261104_0058"
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
    # A cleared blob row stays for the 410 of /files, so the sweep skips it by index.
    op.create_index(
        "ix_cached_file_blobs_retention_candidates",
        "cached_file_blobs",
        ["expires_at"],
        postgresql_where=sa.text("content <> ''"),
    )


def downgrade() -> None:
    op.drop_index("ix_cached_file_blobs_retention_candidates", table_name="cached_file_blobs")
    op.drop_index("ix_runs_data_retention_candidates", table_name="runs")
    op.drop_column("runs", "data_purged_at")
