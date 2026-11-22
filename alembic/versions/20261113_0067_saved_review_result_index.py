"""Keep dashboard saved-result reads indexed after review finalization (#435).

Revision ID: 20261113_0067
Revises: 20261109_0066
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "20261113_0067"
down_revision = "20261109_0066"
branch_labels = None
depends_on = None

_NAME = "ix_run_actions_postprocess_run_index"


def upgrade() -> None:
    op.drop_index(_NAME, table_name="run_actions")
    op.create_index(
        _NAME,
        "run_actions",
        ["run_id", sa.text("index DESC")],
        postgresql_where=sa.text("tool IN ('review.finalize', 'review.postprocess')"),
    )


def downgrade() -> None:
    op.drop_index(_NAME, table_name="run_actions")
    op.create_index(
        _NAME,
        "run_actions",
        ["run_id", sa.text("index DESC")],
        postgresql_where=sa.text("tool = 'review.postprocess'"),
    )
