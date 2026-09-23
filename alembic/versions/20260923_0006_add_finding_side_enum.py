"""Use a PostgreSQL enum for finding sides.

Revision ID: 20260923_0006
Revises: 20260923_0005
Create Date: 2026-09-23
"""

from alembic import op

revision = "20260923_0006"
down_revision = "20260923_0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE TYPE finding_side AS ENUM ('LEFT', 'RIGHT')")
    op.execute("ALTER TABLE findings ALTER COLUMN side DROP DEFAULT")
    op.execute(
        "ALTER TABLE findings ALTER COLUMN side TYPE finding_side USING side::finding_side"
    )
    op.execute("ALTER TABLE findings ALTER COLUMN side SET DEFAULT 'RIGHT'::finding_side")


def downgrade() -> None:
    op.execute("ALTER TABLE findings ALTER COLUMN side DROP DEFAULT")
    op.execute("ALTER TABLE findings ALTER COLUMN side TYPE VARCHAR(10) USING side::text")
    op.execute("ALTER TABLE findings ALTER COLUMN side SET DEFAULT 'RIGHT'")
    op.execute("DROP TYPE finding_side")
