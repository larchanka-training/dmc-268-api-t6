"""Retain sanitized HTTP status for code index source diagnostics.

Revision ID: 20261027_0052
Revises: 20261027_0051
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "20261027_0052"
down_revision = "20261027_0051"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("code_index_states", sa.Column("last_error_http_status", sa.Integer()))


def downgrade() -> None:
    op.drop_column("code_index_states", "last_error_http_status")
