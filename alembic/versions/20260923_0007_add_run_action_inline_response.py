"""Store small run action responses inline.

Revision ID: 20260923_0007
Revises: 20260923_0006
Create Date: 2026-09-23
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "20260923_0007"
down_revision = "20260923_0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "run_actions",
        sa.Column("response", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.create_check_constraint(
        "ck_run_actions_response_location",
        "run_actions",
        "response IS NULL OR response_ref IS NULL",
    )


def downgrade() -> None:
    op.drop_constraint("ck_run_actions_response_location", "run_actions", type_="check")
    op.drop_column("run_actions", "response")
