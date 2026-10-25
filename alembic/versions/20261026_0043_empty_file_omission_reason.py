"""Allow the validated empty-file omission in durable diff snapshots (#213).

Revision ID: 20261026_0043
Revises: 20261026_0042
"""

from __future__ import annotations

from alembic import op

revision = "20261026_0043"
down_revision = "20261026_0042"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint("ck_code_change_diffs_omission_reason", "code_change_diffs", type_="check")
    op.create_check_constraint(
        "ck_code_change_diffs_omission_reason",
        "code_change_diffs",
        "omission_reason IS NULL OR omission_reason IN "
        "('binary', 'too_large', 'generated', 'missing_patch', 'empty_file')",
    )


def downgrade() -> None:
    # Older code lacks the proof contract: retain the row as incomplete on rollback.
    op.execute(
        "UPDATE code_change_diffs SET omission_reason = 'missing_patch' "
        "WHERE omission_reason = 'empty_file'"
    )
    op.drop_constraint("ck_code_change_diffs_omission_reason", "code_change_diffs", type_="check")
    op.create_check_constraint(
        "ck_code_change_diffs_omission_reason",
        "code_change_diffs",
        "omission_reason IS NULL OR omission_reason IN "
        "('binary', 'too_large', 'generated', 'missing_patch')",
    )
