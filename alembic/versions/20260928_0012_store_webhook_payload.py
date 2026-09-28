"""Store durable GitHub webhook JSON payloads alongside legacy S3 references.

Revision ID: 20260928_0012
Revises: 20260925_0011
Create Date: 2026-09-28
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "20260928_0012"
down_revision = "20260925_0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "webhook_events",
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.add_column("webhook_events", sa.Column("projection_claim_token", sa.Uuid(), nullable=True))
    op.add_column("webhook_events", sa.Column("projection_lease_until", sa.DateTime(timezone=True)))
    op.add_column("webhook_events", sa.Column("retry_after", sa.DateTime(timezone=True)))
    op.add_column("webhook_events", sa.Column("projected_at", sa.DateTime(timezone=True)))
    op.alter_column("webhook_events", "payload_s3_ref", existing_type=sa.TEXT(), nullable=True)
    op.alter_column(
        "webhook_events", "installation_external_id", existing_type=sa.BIGINT(), nullable=True
    )
    op.create_check_constraint(
        "ck_webhook_events_payload_present",
        "webhook_events",
        "payload IS NOT NULL OR payload_s3_ref IS NOT NULL",
    )
    op.create_check_constraint(
        "ck_webhook_events_projection_claim_pair",
        "webhook_events",
        "(projection_claim_token IS NULL) = (projection_lease_until IS NULL)",
    )
    op.create_index(
        "ix_webhook_events_pending_retry",
        "webhook_events",
        ["retry_after", "received_at"],
        postgresql_where=sa.text("projected_at IS NULL AND payload IS NOT NULL"),
    )


def downgrade() -> None:
    op.execute(
        "DO $$ BEGIN IF EXISTS ("
        "SELECT 1 FROM webhook_events "
        "WHERE payload IS NOT NULL "
        "OR installation_external_id IS NULL OR payload_s3_ref IS NULL"
        ") THEN RAISE EXCEPTION "
        "'Cannot downgrade webhook payloads while JSONB receipts exist'; "
        "END IF; END $$"
    )
    op.drop_index("ix_webhook_events_pending_retry", table_name="webhook_events")
    op.drop_constraint("ck_webhook_events_projection_claim_pair", "webhook_events", type_="check")
    op.drop_constraint("ck_webhook_events_payload_present", "webhook_events", type_="check")
    op.alter_column(
        "webhook_events", "installation_external_id", existing_type=sa.BIGINT(), nullable=False
    )
    op.alter_column("webhook_events", "payload_s3_ref", existing_type=sa.TEXT(), nullable=False)
    op.drop_column("webhook_events", "projected_at")
    op.drop_column("webhook_events", "retry_after")
    op.drop_column("webhook_events", "projection_lease_until")
    op.drop_column("webhook_events", "projection_claim_token")
    op.drop_column("webhook_events", "payload")
