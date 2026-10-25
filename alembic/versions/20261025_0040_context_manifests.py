"""Store the manifest of every snapshot-path provider call (#130).

Revision ID: 20261025_0040
Revises: 20261024_0039
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "20261025_0040"
down_revision = "20261024_0039"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Additive: no live run writes a manifest until a run binds a snapshot-contract prompt.
    op.create_table(
        "context_manifests",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("attempt", sa.INTEGER(), nullable=False),
        sa.Column("call_id", sa.Uuid(), nullable=False),
        sa.Column("stage", sa.String(length=20), nullable=False),
        sa.Column("recovery_kind", sa.String(length=20), nullable=False),
        sa.Column("parent_call_id", sa.Uuid(), nullable=True),
        sa.Column("policy_snapshot_id", sa.Uuid(), nullable=False),
        sa.Column("digest", sa.CHAR(length=64), nullable=False),
        sa.Column("rendered_messages_sha256", sa.CHAR(length=64), nullable=False),
        sa.Column("prompt_checksum", sa.CHAR(length=64), nullable=False),
        sa.Column("model_profile_version", sa.CHAR(length=64), nullable=False),
        sa.Column("block_count", sa.INTEGER(), nullable=False),
        sa.Column("input_tokens", sa.INTEGER(), nullable=False),
        sa.Column("blocks", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "omissions",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("schema_version", sa.INTEGER(), server_default=sa.text("1"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "stage IN ('conventions', 'review', 'verification')",
            name="ck_context_manifests_stage",
        ),
        sa.CheckConstraint(
            "recovery_kind IN ('none', 'retry', 'repair', 'fallback')",
            name="ck_context_manifests_recovery_kind",
        ),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["policy_snapshot_id"], ["run_policy_snapshots.snapshot_id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("call_id"),
    )
    op.create_index("ix_context_manifests_run_attempt", "context_manifests", ["run_id", "attempt"])


def downgrade() -> None:
    op.drop_index("ix_context_manifests_run_attempt", table_name="context_manifests")
    op.drop_table("context_manifests")
