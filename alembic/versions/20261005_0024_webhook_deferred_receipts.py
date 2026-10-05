"""Stop retrying deferred GitHub deliveries after three attempts.

A deferred delivery (unknown installation or repository, or an event without a
handler) used to be retried every five minutes forever. It now gets a final
``projection_deferred_at`` after three attempts; linking the installation clears it.

Revision ID: 20261005_0024
Revises: 20260930_0023
Create Date: 2026-10-05
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "20261005_0024"
down_revision = "20260930_0023"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "webhook_events",
        sa.Column("projection_deferred_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.drop_index("ix_webhook_events_pending_retry", table_name="webhook_events")
    op.create_index(
        "ix_webhook_events_pending_retry",
        "webhook_events",
        ["retry_after", "received_at"],
        postgresql_where=sa.text(
            "projected_at IS NULL AND projection_failed_at IS NULL "
            "AND projection_deferred_at IS NULL AND payload IS NOT NULL"
        ),
    )


def downgrade() -> None:
    op.drop_index("ix_webhook_events_pending_retry", table_name="webhook_events")
    op.create_index(
        "ix_webhook_events_pending_retry",
        "webhook_events",
        ["retry_after", "received_at"],
        postgresql_where=sa.text(
            "projected_at IS NULL AND projection_failed_at IS NULL AND payload IS NOT NULL"
        ),
    )
    op.drop_column("webhook_events", "projection_deferred_at")
