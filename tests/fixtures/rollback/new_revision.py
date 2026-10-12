"""Synthetic child used only in image B by scripts/verify_rollback.py."""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "__FIXTURE_REVISION__"
down_revision = "__PARENT_REVISION__"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table("rollback110_fixture", sa.Column("sentinel", sa.Text(), nullable=False))


def downgrade() -> None:
    op.drop_table("rollback110_fixture")
