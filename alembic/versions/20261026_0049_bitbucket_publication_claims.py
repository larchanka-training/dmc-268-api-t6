"""Serialize Bitbucket review publication without holding a DB transaction over HTTP (#333).

Revision ID: 20261026_0049
Revises: 20261026_0048
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "20261026_0049"
down_revision = "20261026_0048"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "bitbucket_publication_claims",
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("owner_id", sa.Uuid(), nullable=False),
        sa.Column("generation", sa.BigInteger(), nullable=False),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("run_id"),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"], ondelete="CASCADE"),
        sa.CheckConstraint("generation > 0", name="ck_bitbucket_publication_claim_generation"),
    )


def downgrade() -> None:
    op.execute("LOCK TABLE bitbucket_publication_claims IN ACCESS EXCLUSIVE MODE")
    if op.get_bind().scalar(sa.text("SELECT EXISTS (SELECT 1 FROM bitbucket_publication_claims)")):
        raise RuntimeError("Bitbucket publication claims must be removed before downgrade")
    op.drop_table("bitbucket_publication_claims")
