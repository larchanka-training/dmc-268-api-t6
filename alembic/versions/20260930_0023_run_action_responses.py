"""Store run action responses over 64 KB in their own table.

Revision ID: 20260930_0023
Revises: 20260930_0022
Create Date: 2026-09-30
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "20260930_0023"
down_revision = "20260930_0022"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "run_action_responses",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("body", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_run_action_responses_run_id", "run_action_responses", ["run_id"])


def downgrade() -> None:
    op.drop_index("ix_run_action_responses_run_id", table_name="run_action_responses")
    op.drop_table("run_action_responses")
