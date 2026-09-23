"""Add RuleVersion integrity constraints.

Revision ID: 20260923_0008
Revises: 20260923_0007
Create Date: 2026-09-23
"""

from alembic import op

revision = "20260923_0008"
down_revision = "20260923_0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_check_constraint("ck_rule_versions_version_positive", "rule_versions", "version > 0")
    op.create_check_constraint(
        "ck_rule_versions_checksum_sha256", "rule_versions", "checksum ~ '^[0-9a-f]{64}$'"
    )


def downgrade() -> None:
    op.drop_constraint("ck_rule_versions_checksum_sha256", "rule_versions", type_="check")
    op.drop_constraint("ck_rule_versions_version_positive", "rule_versions", type_="check")
