"""Store the last read of .review/rules.md on the default branch of a repository (#150).

Revision ID: 20261018_0035
Revises: 20261017_0034
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "20261018_0035"
down_revision = "20261017_0034"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "repository_rules_status",
        sa.Column("repository_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(16), nullable=True),
        sa.Column("branch", sa.String(255), nullable=True),
        sa.Column("sha", sa.String(40), nullable=True),
        sa.Column("text", sa.TEXT(), nullable=True),
        sa.Column("checksum", sa.CHAR(64), nullable=True),
        sa.Column("ignore_masks", postgresql.JSONB(), nullable=True),
        sa.Column(
            "diagnostics", postgresql.JSONB(), server_default=sa.text("'[]'"), nullable=False
        ),
        sa.Column("defaults_version", sa.String(64), nullable=True),
        sa.Column("checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("refresh_requested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("refresh_claimed_until", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["repository_id"], ["repositories.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("repository_id"),
        sa.CheckConstraint(
            "status IN ('present', 'missing', 'invalid', 'unavailable')",
            name="ck_repository_rules_status_status",
        ),
        sa.CheckConstraint(
            "num_nulls(status, branch, checked_at) IN (0, 3)",
            name="ck_repository_rules_status_read_complete",
        ),
        sa.CheckConstraint(
            "sha IS NULL OR sha ~ '^[0-9a-f]{40}$'",
            name="ck_repository_rules_status_sha",
        ),
        sa.CheckConstraint(
            "checksum IS NULL OR checksum ~ '^[0-9a-f]{64}$'",
            name="ck_repository_rules_status_checksum",
        ),
        sa.CheckConstraint(
            "status <> 'present' OR num_nulls(sha, text, checksum, ignore_masks) = 0",
            name="ck_repository_rules_status_present",
        ),
        sa.CheckConstraint(
            "status <> 'missing' OR num_nulls(sha, defaults_version) = 0",
            name="ck_repository_rules_status_missing",
        ),
        sa.CheckConstraint(
            "status NOT IN ('invalid', 'unavailable') OR jsonb_array_length(diagnostics) > 0",
            name="ck_repository_rules_status_diagnosed",
        ),
    )
    op.create_index(
        "ix_repository_rules_status_refresh",
        "repository_rules_status",
        ["refresh_requested_at"],
        postgresql_where=sa.text("refresh_requested_at IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_repository_rules_status_refresh", table_name="repository_rules_status")
    op.drop_table("repository_rules_status")
