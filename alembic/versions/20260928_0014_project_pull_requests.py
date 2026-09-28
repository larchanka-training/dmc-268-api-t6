"""Record PR author, reviewer intent, bot assignment, and current-head clock.

Revision ID: 20260928_0014
Revises: 20260928_0013
Create Date: 2026-09-28
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "20260928_0014"
down_revision = "20260928_0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("code_changes", sa.Column("author_login", sa.String(length=255), nullable=True))
    op.add_column("code_changes", sa.Column("reviewer_requested_at", sa.DateTime(timezone=True)))
    op.add_column(
        "code_changes", sa.Column("reviewer_intent_updated_at", sa.DateTime(timezone=True))
    )
    op.add_column("code_changes", sa.Column("reviewer_timeline_event_id", sa.BigInteger()))
    op.add_column("code_changes", sa.Column("reviewer_timeline_position", sa.BigInteger()))
    op.add_column("code_changes", sa.Column("reviewer_barrier_at", sa.DateTime(timezone=True)))
    op.add_column("code_changes", sa.Column("reviewer_barrier_position", sa.BigInteger()))
    op.add_column("code_changes", sa.Column("head_first_seen_at", sa.DateTime(timezone=True)))
    op.add_column("code_changes", sa.Column("provider_updated_at", sa.DateTime(timezone=True)))


def downgrade() -> None:
    op.drop_column("code_changes", "provider_updated_at")
    op.drop_column("code_changes", "head_first_seen_at")
    op.drop_column("code_changes", "reviewer_barrier_at")
    op.drop_column("code_changes", "reviewer_barrier_position")
    op.drop_column("code_changes", "reviewer_timeline_position")
    op.drop_column("code_changes", "reviewer_timeline_event_id")
    op.drop_column("code_changes", "reviewer_intent_updated_at")
    op.drop_column("code_changes", "reviewer_requested_at")
    op.drop_column("code_changes", "author_login")
