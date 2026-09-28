"""Pin webhook run base ref and enforce one run per PR/head with publication recovery.

Revision ID: 20260928_0015
Revises: 20260928_0014
Create Date: 2026-09-28
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "20260928_0015"
down_revision = "20260928_0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("runs", sa.Column("base_ref", sa.String(length=255), nullable=True))
    op.add_column("runs", sa.Column("message_published_at", sa.DateTime(timezone=True)))
    op.create_index(
        "uq_runs_webhook_code_change_head",
        "runs",
        ["code_change_id", "head_sha"],
        unique=True,
        postgresql_where=sa.text("trigger = 'webhook'"),
    )
    op.create_index(
        "ix_runs_pending_webhook_publication",
        "runs",
        ["created_at", "id"],
        postgresql_where=sa.text(
            "trigger = 'webhook' AND state = 'queued' AND message_published_at IS NULL"
        ),
    )


def downgrade() -> None:
    op.drop_index("ix_runs_pending_webhook_publication", table_name="runs")
    op.drop_index("uq_runs_webhook_code_change_head", table_name="runs")
    op.drop_column("runs", "message_published_at")
    op.drop_column("runs", "base_ref")
