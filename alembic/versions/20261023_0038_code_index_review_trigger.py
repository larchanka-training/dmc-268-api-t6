"""Add the review full-build throttle of the index state row (#144).

Revision ID: 20261023_0038
Revises: 20261020_0037
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "20261023_0038"
down_revision = "20261020_0037"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "code_index_states", sa.Column("review_full_requested_at", sa.DateTime(timezone=True))
    )
    op.add_column("code_index_states", sa.Column("review_full_requested_sha", sa.String(64)))
    # A sha implies a time: a request for a NULL target records only the time.
    op.create_check_constraint(
        "ck_code_index_states_review_full_sha_at",
        "code_index_states",
        "review_full_requested_sha IS NULL OR review_full_requested_at IS NOT NULL",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_code_index_states_review_full_sha_at", "code_index_states", type_="check"
    )
    op.drop_column("code_index_states", "review_full_requested_sha")
    op.drop_column("code_index_states", "review_full_requested_at")
