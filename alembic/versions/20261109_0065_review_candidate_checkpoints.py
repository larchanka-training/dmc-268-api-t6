"""Keep accepted review candidates outside the capped public action trace (#435).

Revision ID: 20261109_0065
Revises: 20261109_0064
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "20261109_0065"
down_revision = "20261109_0064"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "review_candidate_checkpoints",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("attempt", sa.INTEGER(), nullable=False),
        sa.Column("accepted_output", postgresql.JSONB(), nullable=False),
        sa.Column("fitted_context", postgresql.JSONB(), nullable=False),
        sa.Column("candidates", postgresql.JSONB(), nullable=False),
        sa.Column("dropped", postgresql.JSONB(), nullable=False),
        sa.Column("incomplete_paths", postgresql.JSONB(), nullable=False),
        sa.Column("accepted_call_id", sa.Uuid()),
        sa.Column("payload_sha256", sa.CHAR(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "run_id", "attempt", name="uq_review_candidate_checkpoints_run_attempt"
        ),
    )


def downgrade() -> None:
    op.drop_table("review_candidate_checkpoints")
