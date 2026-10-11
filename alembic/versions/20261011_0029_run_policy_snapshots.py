"""Store the policy documents and the immutable policy snapshot of a run.

Revision ID: 20261011_0029
Revises: 20261007_0028
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "20261011_0029"
down_revision = "20261007_0028"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "run_policy_documents",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "run_id", sa.Uuid(), sa.ForeignKey("runs.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("path", sa.String(255), nullable=False),
        sa.Column("presence", sa.String(16), nullable=False),
        sa.Column("blob_oid", sa.String(64)),
        sa.Column("raw_sha256", sa.CHAR(64)),
        sa.Column("text_sha256", sa.CHAR(64)),
        sa.Column("raw_bytes", sa.LargeBinary()),
        sa.Column("text", sa.TEXT()),
        sa.Column("byte_count", sa.INTEGER()),
        sa.Column("line_count", sa.INTEGER()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint("run_id", "path", name="uq_run_policy_documents_run_path"),
        sa.CheckConstraint(
            "presence IN ('present', 'missing')", name="ck_run_policy_documents_presence"
        ),
        sa.CheckConstraint(
            "(presence = 'missing') = (text IS NULL) "
            "AND (presence = 'missing') = (raw_bytes IS NULL) "
            "AND (presence = 'missing') = (raw_sha256 IS NULL) "
            "AND (presence = 'missing') = (text_sha256 IS NULL) "
            "AND (presence = 'missing') = (blob_oid IS NULL)",
            name="ck_run_policy_documents_content_matches_presence",
        ),
    )
    op.create_table(
        "run_policy_snapshots",
        sa.Column(
            "run_id", sa.Uuid(), sa.ForeignKey("runs.id", ondelete="CASCADE"), primary_key=True
        ),
        sa.Column("snapshot_id", sa.Uuid(), nullable=False, unique=True),
        sa.Column(
            "repository_id",
            sa.Uuid(),
            sa.ForeignKey("repositories.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("schema_version", sa.INTEGER(), nullable=False),
        sa.Column("policy_base_sha", sa.String(64), nullable=False),
        sa.Column("ignore_patterns", postgresql.JSONB(), nullable=False),
        sa.Column("ignore_digest", sa.CHAR(64), nullable=False),
        sa.Column("parser_profile", sa.String(64), nullable=False),
        sa.Column("parser_version", sa.String(64), nullable=False),
        sa.Column("matcher_version", sa.String(64), nullable=False),
        sa.Column("limits_version", sa.String(64), nullable=False),
        sa.Column("builtin_exclusions_version", sa.String(64), nullable=False),
        sa.Column("priority_matcher_version", sa.String(64), nullable=False),
        sa.Column(
            "service_defaults_rule_version_id",
            sa.Uuid(),
            sa.ForeignKey("rule_versions.id"),
            nullable=False,
        ),
        sa.Column("defaults_checksum", sa.CHAR(64), nullable=False),
        sa.Column("prompt_bindings", postgresql.JSONB(), nullable=False),
        sa.Column("execution_profile", postgresql.JSONB(), nullable=False),
        sa.Column("policy_digest", sa.CHAR(64), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )


def downgrade() -> None:
    op.drop_table("run_policy_snapshots")
    op.drop_table("run_policy_documents")
