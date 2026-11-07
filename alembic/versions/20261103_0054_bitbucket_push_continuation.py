"""Persist the completed Bitbucket push candidate page position (#360).

Revision ID: 20261103_0054
Revises: 20261103_0053
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "20261103_0054"
down_revision = "20261103_0053"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "webhook_events",
        sa.Column("bitbucket_push_after_pr_number", sa.BigInteger(), nullable=True),
    )
    op.create_check_constraint(
        "ck_webhook_events_bitbucket_push_position",
        "webhook_events",
        "bitbucket_push_after_pr_number IS NULL OR "
        "(provider = 'bitbucket' AND event = 'repo:push' "
        "AND bitbucket_push_after_pr_number > 0)",
    )


def downgrade() -> None:
    op.drop_constraint("ck_webhook_events_bitbucket_push_position", "webhook_events", type_="check")
    op.drop_column("webhook_events", "bitbucket_push_after_pr_number")
