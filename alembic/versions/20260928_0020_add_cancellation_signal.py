"""Retain attempted Run cancellation signals until publisher confirmation.

Revision ID: 20260928_0020
Revises: 20260928_0019
Create Date: 2026-09-28
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "20260928_0020"
down_revision = "20260928_0019"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("runs", sa.Column("cancellation_signal_requested_at", sa.DateTime(timezone=True)))
    op.add_column("runs", sa.Column("cancellation_signal_published_at", sa.DateTime(timezone=True)))
    op.create_index(
        "ix_runs_pending_cancellation_signal",
        "runs",
        ["cancellation_signal_requested_at", "id"],
        postgresql_where=sa.text(
            "cancellation_signal_requested_at IS NOT NULL "
            "AND cancellation_signal_published_at IS NULL"
        ),
    )


def downgrade() -> None:
    op.drop_index("ix_runs_pending_cancellation_signal", table_name="runs")
    op.drop_column("runs", "cancellation_signal_published_at")
    op.drop_column("runs", "cancellation_signal_requested_at")
