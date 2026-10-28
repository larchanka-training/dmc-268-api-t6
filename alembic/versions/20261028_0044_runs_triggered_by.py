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


def downgrade() -> None:
    op.drop_column("runs", "triggered_by")
