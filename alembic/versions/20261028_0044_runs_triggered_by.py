"""Add nullable runs.triggered_by for the actor who started a run.

Revision ID: 20261028_0044
Revises: 20261026_0043
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "20261028_0044"
down_revision = "20261026_0043"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("runs", sa.Column("triggered_by", sa.String(length=255), nullable=True))
    op.create_index(
        "ix_webhook_events_pull_request_head",
        "webhook_events",
        ["installation_external_id", sa.text("(payload #>> '{pull_request,head,sha}'::text[])")],
        postgresql_where=sa.text(
            "event = 'pull_request' AND action = 'synchronize' AND payload IS NOT NULL"
        ),
    )
    op.create_index(
        "ix_webhook_events_push_after",
        "webhook_events",
        ["installation_external_id", sa.text("(payload #>> '{after}'::text[])")],
        postgresql_where=sa.text("event = 'push' AND payload IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_webhook_events_push_after", table_name="webhook_events")
    op.drop_index("ix_webhook_events_pull_request_head", table_name="webhook_events")
    op.drop_column("runs", "triggered_by")
