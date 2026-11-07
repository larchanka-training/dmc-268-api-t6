"""Persist final review analysis and publication plan before T8 (#435).

Revision ID: 20261107_0060
Revises: 20261107_0059
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "20261107_0060"
down_revision = "20261107_0059"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "final_review_results",
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("source_attempt", sa.INTEGER(), nullable=False),
        sa.Column("accepted_review_call_id", sa.Uuid()),
        sa.Column("accepted_verifier_call_id", sa.Uuid()),
        sa.Column("result", postgresql.JSONB(), nullable=False),
        sa.Column("publication_plan", postgresql.JSONB(), nullable=False),
        sa.Column("digest", sa.CHAR(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("run_id"),
    )


def downgrade() -> None:
    op.drop_table("final_review_results")
