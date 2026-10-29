"""Durable Bitbucket build status intent and per-key external write lease (#333).

Revision ID: 20261027_0050
Revises: 20261026_0049
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "20261027_0050"
down_revision = "20261026_0049"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "bitbucket_run_origins",
        sa.Column("run_order", sa.BigInteger(), sa.Identity(always=True), nullable=False),
    )
    op.create_unique_constraint(
        "uq_bitbucket_run_origins_order", "bitbucket_run_origins", ["run_order"]
    )
    op.create_table(
        "bitbucket_status_publications",
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("repository_uuid", sa.Uuid(), nullable=False),
        sa.Column("pr_number", sa.Integer(), nullable=False),
        sa.Column("head_sha", sa.String(40), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("run_order", sa.BigInteger(), nullable=False),
        sa.Column("run_url", sa.Text(), nullable=False),
        sa.Column("desired_state", sa.String(16), nullable=False),
        sa.Column("desired_rank", sa.Integer(), nullable=False),
        sa.Column("applied_run_id", sa.Uuid()),
        sa.Column("applied_state", sa.String(16)),
        sa.Column("blocked_generation", sa.BigInteger()),
        sa.Column("last_error", sa.String(64)),
        sa.Column("owner_id", sa.Uuid()),
        sa.Column("generation", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("lease_until", sa.DateTime(timezone=True)),
        sa.Column(
            "next_check_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.PrimaryKeyConstraint("workspace_id", "repository_uuid", "pr_number", "head_sha"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"], ondelete="CASCADE"),
        sa.CheckConstraint("pr_number > 0", name="ck_bitbucket_status_publications_pr_number"),
        sa.CheckConstraint("generation >= 0", name="ck_bitbucket_status_publications_generation"),
        sa.CheckConstraint(
            "desired_rank BETWEEN 0 AND 3", name="ck_bitbucket_status_publications_rank"
        ),
    )
    op.create_index(
        "ix_bitbucket_status_publications_replay",
        "bitbucket_status_publications",
        ["lease_until", "run_id"],
    )


def downgrade() -> None:
    op.execute("LOCK TABLE bitbucket_status_publications IN ACCESS EXCLUSIVE MODE")
    if op.get_bind().scalar(sa.text("SELECT EXISTS (SELECT 1 FROM bitbucket_status_publications)")):
        raise RuntimeError("Bitbucket status intents must be removed before downgrade")
    op.drop_index(
        "ix_bitbucket_status_publications_replay", table_name="bitbucket_status_publications"
    )
    op.drop_table("bitbucket_status_publications")
    op.drop_constraint("uq_bitbucket_run_origins_order", "bitbucket_run_origins", type_="unique")
    op.drop_column("bitbucket_run_origins", "run_order")
