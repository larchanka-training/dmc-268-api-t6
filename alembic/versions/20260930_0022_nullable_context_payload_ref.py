"""Allow context payload summaries without an object-storage reference.

The full ContextPayload is not stored in the MVP (no object storage, D1): the
worker's ``context.build`` step writes only the summary.

Revision ID: 20260930_0022
Revises: 20260929_0021
Create Date: 2026-09-30
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "20260930_0022"
down_revision = "20260929_0021"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column("context_payloads", "s3_ref", existing_type=sa.Text(), nullable=True)


def downgrade() -> None:
    op.execute("DELETE FROM context_payloads WHERE s3_ref IS NULL")
    op.alter_column("context_payloads", "s3_ref", existing_type=sa.Text(), nullable=False)
