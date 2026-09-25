"""Store per-file unified diff snapshots in PostgreSQL.

Revision ID: 20260924_0008
Revises: 20260923_0007
Create Date: 2026-09-24
"""

import sqlalchemy as sa

from alembic import op

revision = "20260924_0008"
down_revision = "20260923_0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "code_change_diffs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("code_change_id", sa.Uuid(), nullable=False),
        sa.Column("head_sha", sa.String(length=64), nullable=False),
        sa.Column("filename", sa.String(length=1024), nullable=False),
        sa.Column("patch", sa.TEXT(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["code_change_id"], ["code_changes.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("code_change_id", "head_sha", "filename"),
    )


def downgrade() -> None:
    op.drop_table("code_change_diffs")
