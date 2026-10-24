"""Pin the conventions and verifier prompts to runs (#130).

Revision ID: 20261024_0039
Revises: 20261024_0038
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "20261024_0039"
down_revision = "20261024_0038"
branch_labels = None
depends_on = None

_PIN_COLUMNS = ("conventions_prompt_version_id", "verifier_prompt_version_id")


def upgrade() -> None:
    # Historical runs never pinned these prompts and deliberately remain NULL.
    for column in _PIN_COLUMNS:
        op.add_column("runs", sa.Column(column, sa.Uuid(), nullable=True))
        op.create_foreign_key(f"runs_{column}_fkey", "runs", "prompt_versions", [column], ["id"])


def downgrade() -> None:
    for column in reversed(_PIN_COLUMNS):
        op.drop_constraint(f"runs_{column}_fkey", "runs", type_="foreignkey")
        op.drop_column("runs", column)
