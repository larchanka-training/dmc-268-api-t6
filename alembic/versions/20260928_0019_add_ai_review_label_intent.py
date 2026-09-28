"""Add label intent without inheriting obsolete reviewer requests.

Revision ID: 20260928_0019
Revises: 20260928_0018
Create Date: 2026-09-28
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "20260928_0019"
down_revision = "20260928_0018"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # An old reviewer_requested=true row is not evidence of the ai-review label.
    op.add_column(
        "code_changes",
        sa.Column("ai_review_labeled", sa.Boolean(), server_default=sa.false(), nullable=False),
    )
    op.add_column("code_changes", sa.Column("ai_review_labeled_at", sa.DateTime(timezone=True)))
    op.add_column("code_changes", sa.Column("label_intent_updated_at", sa.DateTime(timezone=True)))


def downgrade() -> None:
    op.drop_column("code_changes", "label_intent_updated_at")
    op.drop_column("code_changes", "ai_review_labeled_at")
    op.drop_column("code_changes", "ai_review_labeled")
