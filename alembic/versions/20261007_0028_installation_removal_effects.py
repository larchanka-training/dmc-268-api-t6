"""Deduplicate installation removal effects independently of receipt finalization.

Revision ID: 20261007_0028
Revises: 20261007_0027
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "20261007_0028"
down_revision = "20261007_0027"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "github_installation_removal_effects",
        sa.Column("delivery_id", sa.String(255), primary_key=True),
    )


def downgrade() -> None:
    op.drop_table("github_installation_removal_effects")
