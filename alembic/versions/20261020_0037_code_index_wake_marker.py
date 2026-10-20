"""Add the wake-up marker of the index state row and the closed-file index (#143).

Revision ID: 20261020_0037
Revises: 20261019_0036
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "20261020_0037"
down_revision = "20261019_0036"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("code_index_states", sa.Column("wake_requested_at", sa.DateTime(timezone=True)))
    # Only a live index waits for a wake-up: a purge and a failure clear the marker.
    op.create_check_constraint(
        "ck_code_index_states_wake_live",
        "code_index_states",
        "wake_requested_at IS NULL OR (purge_reason IS NULL AND status <> 'failed')",
    )
    op.create_index(
        "ix_code_index_states_wake_requested",
        "code_index_states",
        ["wake_requested_at"],
        postgresql_where=sa.text("wake_requested_at IS NOT NULL"),
    )
    op.create_index(
        "ix_code_index_files_closed",
        "code_index_files",
        ["repository_id"],
        postgresql_where=sa.text("valid_to IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_code_index_files_closed", table_name="code_index_files")
    op.drop_index("ix_code_index_states_wake_requested", table_name="code_index_states")
    op.drop_constraint("ck_code_index_states_wake_live", "code_index_states", type_="check")
    op.drop_column("code_index_states", "wake_requested_at")
