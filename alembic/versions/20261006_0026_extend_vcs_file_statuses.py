"""Extend code_change_diffs status check constraint to include copied, changed, and unchanged.

Revision ID: 20261006_0026
Revises: 20261005_0025
Create Date: 2026-10-06
"""

from __future__ import annotations

from alembic import op

revision = "20261006_0026"
down_revision = "20261005_0025"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint("ck_code_change_diffs_status", "code_change_diffs")
    op.create_check_constraint(
        "ck_code_change_diffs_status",
        "code_change_diffs",
        "status IN ('added', 'modified', 'removed', 'renamed', 'copied', 'changed', 'unchanged')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_code_change_diffs_status", "code_change_diffs")
    op.create_check_constraint(
        "ck_code_change_diffs_status",
        "code_change_diffs",
        "status IN ('added', 'modified', 'removed', 'renamed')",
    )
