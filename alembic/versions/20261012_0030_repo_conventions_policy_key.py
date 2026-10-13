"""Key cached repository conventions by the ignore policy they were generated under.

Revision ID: 20261012_0030
Revises: 20261011_0029
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "20261012_0030"
down_revision = "20261011_0029"
branch_labels = None
depends_on = None

_KEY = "uq_repo_conventions_snapshot"


def upgrade() -> None:
    op.add_column("repo_conventions", sa.Column("policy_key", sa.CHAR(64)))
    op.drop_constraint(_KEY, "repo_conventions", type_="unique")
    op.create_unique_constraint(
        _KEY,
        "repo_conventions",
        ["repository_id", "agents_md_sha", "prompt_version_id", "policy_key"],
        postgresql_nulls_not_distinct=True,
    )


def downgrade() -> None:
    # Entries of different policies would collide under the old key.
    op.execute("DELETE FROM repo_conventions WHERE policy_key IS NOT NULL")
    op.drop_constraint(_KEY, "repo_conventions", type_="unique")
    op.create_unique_constraint(
        _KEY,
        "repo_conventions",
        ["repository_id", "agents_md_sha", "prompt_version_id"],
        postgresql_nulls_not_distinct=True,
    )
    op.drop_column("repo_conventions", "policy_key")
