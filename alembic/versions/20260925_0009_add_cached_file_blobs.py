"""Store immutable file blobs shared by workers and the read API.

Revision ID: 20260925_0009
Revises: 20260924_0008
Create Date: 2026-09-25
"""

import sqlalchemy as sa

from alembic import op

revision = "20260925_0009"
down_revision = "20260924_0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "cached_file_blobs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("code_change_id", sa.Uuid(), nullable=False),
        sa.Column("head_sha", sa.String(length=64), nullable=False),
        sa.Column("path", sa.String(length=1024), nullable=False),
        sa.Column("content", sa.TEXT(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["code_change_id"], ["code_changes.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("code_change_id", "head_sha", "path"),
    )


def downgrade() -> None:
    op.drop_table("cached_file_blobs")
