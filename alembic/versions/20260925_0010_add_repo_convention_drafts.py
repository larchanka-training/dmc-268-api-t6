"""Store recoverable model draft files for repository-convention cache hits.

Revision ID: 20260925_0010
Revises: 20260923_0008, 20260925_0009
Create Date: 2026-09-25
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "20260925_0010"
down_revision = ("20260923_0008", "20260925_0009")
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "repo_convention_drafts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("repo_conventions_id", sa.Uuid(), nullable=False),
        sa.Column("files", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["repo_conventions_id"], ["repo_conventions.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("repo_conventions_id"),
    )


def downgrade() -> None:
    op.drop_table("repo_convention_drafts")
