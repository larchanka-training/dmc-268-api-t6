"""Remember whether a repository is private, and find it by name in any case (#454).

Revision ID: 20261106_0053
Revises: 20261027_0052
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "20261106_0053"
down_revision = "20261027_0052"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # No backfill: NULL reads as not public until a webhook tells the visibility.
    op.add_column("repositories", sa.Column("private", sa.Boolean(), nullable=True))
    op.create_index(
        "ix_repositories_lower_full_name", "repositories", [sa.text("lower(full_name)")]
    )


def downgrade() -> None:
    op.drop_index("ix_repositories_lower_full_name", table_name="repositories")
    op.drop_column("repositories", "private")
