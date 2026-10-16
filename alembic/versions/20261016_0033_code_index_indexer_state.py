"""Extend the index state row for the indexer: branch, exclusions, storage bytes (#142).

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

_PROFILE_COLUMNS = (
    "extractor_version",
    "resolver_version",
    "parser_profile_version",
    "limits_profile_version",
    "indexed_default_branch",
    "exclusions_digest",
)
_CHECKS = {
    # A publication records all six versions of its profile; any other row records none.
    "ck_code_index_states_profile_complete": f"num_nulls({', '.join(_PROFILE_COLUMNS)}) IN (0, 6)",
    "ck_code_index_states_bytes": "stored_bytes >= 0 AND pending_bytes >= 0",
    "ck_code_index_states_pending_leased": "pending_bytes = 0 OR lease_token IS NOT NULL",
    "ck_code_index_states_failed_code": (
        "status <> 'failed' OR error_code = 'archive_unsafe' OR error_code LIKE 'limit_exceeded:%'"
    ),
}


def upgrade() -> None:
    op.add_column("code_index_states", sa.Column("indexed_default_branch", sa.String(255)))
    op.add_column("code_index_states", sa.Column("exclusions_digest", sa.String(64)))
    op.add_column(
        "code_index_states",
        sa.Column("stored_bytes", sa.BIGINT(), nullable=False, server_default=sa.text("0")),
    )
    op.add_column(
        "code_index_states",
        sa.Column("pending_bytes", sa.BIGINT(), nullable=False, server_default=sa.text("0")),
    )
    # A publication without a branch and an exclusions digest cannot be compared with
    # HEAD, so the next build of such a row is full.
    op.execute(
        "UPDATE code_index_states SET extractor_version = NULL, resolver_version = NULL, "
        "parser_profile_version = NULL, limits_profile_version = NULL"
    )
    for name, condition in _CHECKS.items():
        op.create_check_constraint(name, "code_index_states", condition)
    op.create_index(
        "ix_code_index_states_live_lease",
        "code_index_states",
        ["repository_id"],
        postgresql_where=sa.text("lease_token IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_code_index_states_live_lease", table_name="code_index_states")
    for name in reversed(_CHECKS):
        op.drop_constraint(name, "code_index_states", type_="check")
    op.drop_column("code_index_states", "pending_bytes")
    op.drop_column("code_index_states", "stored_bytes")
    op.drop_column("code_index_states", "exclusions_digest")
    op.drop_column("code_index_states", "indexed_default_branch")
