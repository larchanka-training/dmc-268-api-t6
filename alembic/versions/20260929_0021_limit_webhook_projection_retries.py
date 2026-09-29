"""Limit unexpected webhook projection failures to three attempts.

Revision ID: 20260929_0021
Revises: 20260928_0020
Create Date: 2026-09-29
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "20260929_0021"
down_revision = "20260928_0020"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "webhook_events",
        sa.Column(
            "projection_attempt_count",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
    )
    op.create_check_constraint(
        "ck_webhook_events_projection_attempt_count_nonnegative",
        "webhook_events",
        "projection_attempt_count >= 0",
    )
    op.add_column(
        "webhook_events",
        sa.Column("projection_failed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.drop_index("ix_webhook_events_pending_retry", table_name="webhook_events")
    op.create_index(
        "ix_webhook_events_pending_retry",
        "webhook_events",
        ["retry_after", "received_at"],
        postgresql_where=sa.text(
            "projected_at IS NULL AND projection_failed_at IS NULL AND payload IS NOT NULL"
        ),
    )


def downgrade() -> None:
    op.drop_index("ix_webhook_events_pending_retry", table_name="webhook_events")
    op.create_index(
        "ix_webhook_events_pending_retry",
        "webhook_events",
        ["retry_after", "received_at"],
        postgresql_where=sa.text("projected_at IS NULL AND payload IS NOT NULL"),
    )
    op.drop_constraint(
        "ck_webhook_events_projection_attempt_count_nonnegative",
        "webhook_events",
        type_="check",
    )
    op.drop_column("webhook_events", "projection_failed_at")
    op.drop_column("webhook_events", "projection_attempt_count")
