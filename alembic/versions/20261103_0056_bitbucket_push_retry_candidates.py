"""Retain deferred push PRs while later candidate pages advance (#360).

Revision ID: 20261103_0056
Revises: 20261103_0055
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "20261103_0056"
down_revision = "20261103_0055"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "bitbucket_push_retry_candidates",
        sa.Column(
            "webhook_event_id",
            sa.Uuid(),
            sa.ForeignKey("webhook_events.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("pr_number", sa.BigInteger(), primary_key=True),
        sa.Column("attempted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("reason", sa.String(length=64), nullable=False),
        sa.Column("failed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("pr_number > 0", name="ck_bitbucket_push_retry_candidates_pr_number"),
        sa.CheckConstraint(
            "attempt_count >= 0", name="ck_bitbucket_push_retry_candidates_attempt_count"
        ),
    )
    op.create_index(
        "ix_bitbucket_push_retry_candidates_attempt",
        "bitbucket_push_retry_candidates",
        ["webhook_event_id", "attempted_at", "pr_number"],
    )
    op.create_index(
        "ix_webhook_events_bitbucket_pending_due",
        "webhook_events",
        [sa.text("COALESCE(retry_after, received_at)"), "id"],
        postgresql_where=sa.text(
            "provider = 'bitbucket' AND payload IS NOT NULL "
            "AND projected_at IS NULL AND projection_failed_at IS NULL "
            "AND projection_deferred_at IS NULL"
        ),
    )


def downgrade() -> None:
    remaining = op.get_bind().scalar(
        sa.text("SELECT count(*) FROM bitbucket_push_retry_candidates")
    )
    if remaining:
        raise RuntimeError(
            "Cannot downgrade 20261103_0056 while Bitbucket push retry candidates remain; "
            "resolve or archive their receipts and drain the retry table first"
        )
    op.drop_index("ix_webhook_events_bitbucket_pending_due", table_name="webhook_events")
    op.drop_index(
        "ix_bitbucket_push_retry_candidates_attempt",
        table_name="bitbucket_push_retry_candidates",
    )
    op.drop_table("bitbucket_push_retry_candidates")
