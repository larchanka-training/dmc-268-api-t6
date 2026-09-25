"""Persist whether a finding is an inline provider comment or review-body only.

Revision ID: 20260925_0011
Revises: 20260925_0010
Create Date: 2026-09-25
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "20260925_0011"
down_revision = "20260925_0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "findings",
        sa.Column("inline_comment", sa.BOOLEAN(), nullable=False, server_default=sa.text("false")),
    )


def downgrade() -> None:
    op.drop_column("findings", "inline_comment")
