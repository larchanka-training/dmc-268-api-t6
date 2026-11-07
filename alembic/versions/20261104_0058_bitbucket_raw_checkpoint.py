"""Persist bounded raw Bitbucket blobs across index retries (#360).

Revision ID: 20261104_0058
Revises: 20261103_0057
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "20261104_0058"
down_revision = "20261103_0057"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "code_index_states",
        sa.Column("raw_checkpoint_bytes", sa.BigInteger(), server_default="0", nullable=False),
    )
    op.create_check_constraint(
        "ck_code_index_states_raw_checkpoint_bytes",
        "code_index_states",
        "raw_checkpoint_bytes >= 0 AND raw_checkpoint_bytes <= 104857600",
    )
    op.create_table(
        "bitbucket_raw_checkpoint_blobs",
        sa.Column(
            "repository_id",
            sa.Uuid(),
            sa.ForeignKey("code_index_states.repository_id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("path_key", sa.String(64), primary_key=True),
        sa.Column("target_sha", sa.String(40), nullable=False),
        sa.Column("binding_generation", sa.BigInteger(), nullable=False),
        sa.Column("index_generation", sa.BigInteger(), nullable=False),
        sa.Column("path", sa.String(1024), nullable=False),
        sa.Column("body", sa.LargeBinary(), nullable=False),
        sa.CheckConstraint(
            "binding_generation > 0 AND index_generation >= 0 AND octet_length(body) <= 104857600",
            name="ck_bitbucket_raw_checkpoint_blobs_budget",
        ),
    )


def downgrade() -> None:
    op.drop_table("bitbucket_raw_checkpoint_blobs")
    op.drop_constraint("ck_code_index_states_raw_checkpoint_bytes", "code_index_states")
    op.drop_column("code_index_states", "raw_checkpoint_bytes")
