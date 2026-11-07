"""Keep the rules-file link of an inline GitHub comment on its finding.

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
    op.add_column("findings", sa.Column("rule_link", sa.Text()))


def downgrade() -> None:
    op.drop_column("findings", "rule_link")
