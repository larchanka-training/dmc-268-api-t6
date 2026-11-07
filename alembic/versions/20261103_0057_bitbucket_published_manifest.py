"""Retain a complete published Bitbucket source manifest for sparse pushes (#360).

Revision ID: 20261103_0057
Revises: 20261103_0056
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "20261103_0057"
down_revision = "20261103_0056"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "bitbucket_published_manifests",
        sa.Column(
            "repository_id",
            sa.Uuid(),
            sa.ForeignKey("code_index_states.repository_id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("commit_sha", sa.String(40), nullable=False),
        sa.Column("binding_generation", sa.BigInteger(), nullable=False),
        sa.Column("index_generation", sa.BigInteger(), nullable=False),
        sa.Column("entries", postgresql.JSONB(), nullable=False),
        sa.CheckConstraint(
            "binding_generation > 0 AND index_generation >= 0",
            name="ck_bitbucket_published_manifests_generation",
        ),
    )


def downgrade() -> None:
    op.drop_table("bitbucket_published_manifests")
