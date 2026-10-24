"""Pin publication settings to runs and policy snapshots (#212).

Revision ID: 20261024_0038
Revises: 20261023_0038
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "20261024_0038"
down_revision = "20261023_0038"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Repository owns this native enum. Historical rows deliberately remain NULL.
    threshold = postgresql.ENUM(
        "all", "warning", "critical", name="severity_threshold", create_type=False
    )
    for table in ("runs", "run_policy_snapshots"):
        op.add_column(table, sa.Column("severity_threshold", threshold, nullable=True))
        op.add_column(table, sa.Column("max_comments", sa.SmallInteger(), nullable=True))
        op.create_check_constraint(
            f"ck_{table}_publication_settings",
            table,
            "(severity_threshold IS NULL AND max_comments IS NULL) OR "
            "(severity_threshold IS NOT NULL AND max_comments IS NOT NULL AND max_comments > 0)",
        )


def downgrade() -> None:
    for table in ("run_policy_snapshots", "runs"):
        op.drop_constraint(f"ck_{table}_publication_settings", table, type_="check")
        op.drop_column(table, "max_comments")
        op.drop_column(table, "severity_threshold")
