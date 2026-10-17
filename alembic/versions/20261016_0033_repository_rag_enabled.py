"""Store the RAG toggle of a repository, off by default (#145).

Revision ID: 20261016_0033
Revises: 20261015_0032
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "20261016_0033"
down_revision = "20261015_0032"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "repositories",
        sa.Column("rag_enabled", sa.BOOLEAN(), server_default=sa.text("false"), nullable=False),
    )


def downgrade() -> None:
    op.drop_column("repositories", "rag_enabled")
