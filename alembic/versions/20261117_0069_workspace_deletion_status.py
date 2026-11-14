"""Add workspace status, deleted_at, and deleted_by for workspace deletion (#550).

Revision ID: 20261117_0069
Revises: 20261114_0068
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "20261117_0069"
down_revision = "20261114_0068"
branch_labels = None
depends_on = None

_WORKSPACE_STATUSES = ("active", "deleted")


def upgrade() -> None:
    postgresql.ENUM(*_WORKSPACE_STATUSES, name="workspace_status").create(
        op.get_bind(), checkfirst=False
    )
    op.add_column(
        "workspaces",
        sa.Column(
            "status",
            postgresql.ENUM(*_WORKSPACE_STATUSES, name="workspace_status", create_type=False),
            server_default="active",
            nullable=False,
        ),
    )
    op.add_column(
        "workspaces",
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "workspaces",
        sa.Column("deleted_by", sa.BigInteger(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("workspaces", "deleted_by")
    op.drop_column("workspaces", "deleted_at")
    op.drop_column("workspaces", "status")
    postgresql.ENUM(*_WORKSPACE_STATUSES, name="workspace_status").drop(
        op.get_bind(), checkfirst=False
    )
