"""Track unknown line counts on persisted diff snapshots for collapsed GitLab files (#614).

Revision ID: 20261127_0070
Revises: 20261117_0069
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "20261127_0070"
down_revision = "20261117_0069"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "code_change_diffs",
        sa.Column(
            "unknown_line_counts",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )


def downgrade() -> None:
    op.drop_column("code_change_diffs", "unknown_line_counts")
