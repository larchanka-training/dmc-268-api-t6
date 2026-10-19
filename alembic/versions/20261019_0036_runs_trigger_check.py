"""Restrict runs.trigger to the spec values with a CHECK constraint (#174).

Revision ID: 20261019_0036
Revises: 20261018_0035
"""

from __future__ import annotations

from alembic import op

revision = "20261019_0036"
down_revision = "20261018_0035"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_check_constraint(
        "ck_runs_trigger",
        "runs",
        "trigger IN ('webhook', 'manual', 'rerun', 'dry_run')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_runs_trigger", "runs", type_="check")
