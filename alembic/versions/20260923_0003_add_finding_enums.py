"""Use PostgreSQL enums for finding severity and category.

Revision ID: 20260923_0003
Revises: 20260913_0001
Create Date: 2026-09-23
"""

from alembic import op

revision = "20260923_0003"
down_revision = "20260913_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE TYPE finding_severity AS ENUM ('critical', 'high', 'medium', 'low', 'info')")
    op.execute(
        "CREATE TYPE finding_category AS ENUM "
        "('security', 'correctness', 'performance', 'readability')"
    )
    op.execute(
        "ALTER TABLE findings ALTER COLUMN severity TYPE finding_severity "
        "USING severity::finding_severity"
    )
    op.execute(
        "ALTER TABLE findings ALTER COLUMN category TYPE finding_category "
        "USING category::finding_category"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE findings ALTER COLUMN category TYPE VARCHAR(30) USING category::text")
    op.execute("ALTER TABLE findings ALTER COLUMN severity TYPE VARCHAR(30) USING severity::text")
    op.execute("DROP TYPE finding_category")
    op.execute("DROP TYPE finding_severity")
