"""Persist validated Bitbucket pinned tree listing pages (#360).

Revision ID: 20261103_0055
Revises: 20261103_0054
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "20261103_0055"
down_revision = "20261103_0054"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "bitbucket_tree_checkpoint_pages",
        sa.Column("repository_id", sa.Uuid(), nullable=False),
        sa.Column("directory_key", sa.String(64), nullable=False),
        sa.Column("cursor_key", sa.String(64), nullable=False),
        sa.Column("target_sha", sa.String(40), nullable=False),
        sa.Column("binding_generation", sa.BigInteger(), nullable=False),
        sa.Column("index_generation", sa.BigInteger(), nullable=False),
        sa.Column("directory", sa.String(1024), nullable=False),
        sa.Column("cursor", postgresql.JSONB(), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("bytes_read", sa.Integer(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("complete", sa.Boolean(), nullable=False),
        sa.CheckConstraint(
            "binding_generation > 0 AND index_generation >= 0 AND bytes_read >= 0 AND attempts > 0",
            name="ck_bitbucket_tree_checkpoint_pages_budget",
        ),
        sa.ForeignKeyConstraint(
            ["repository_id"], ["code_index_states.repository_id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("repository_id", "directory_key", "cursor_key"),
    )


def downgrade() -> None:
    op.drop_table("bitbucket_tree_checkpoint_pages")
