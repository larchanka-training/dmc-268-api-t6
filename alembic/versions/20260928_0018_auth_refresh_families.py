"""Serialize refresh rotation through a persistent token-family row.

Revision ID: 20260928_0018
Revises: 20260928_0017
Create Date: 2026-09-28
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "20260928_0018"
down_revision = "20260928_0017"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "auth_refresh_families",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "github_user_id",
            sa.BIGINT(),
            sa.ForeignKey("github_user_profiles.id"),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index("ix_auth_refresh_families_user_id", "auth_refresh_families", ["github_user_id"])
    op.execute(
        "DO $$ BEGIN IF EXISTS ("
        "SELECT 1 FROM auth_refresh_sessions GROUP BY family_id "
        "HAVING count(DISTINCT github_user_id) <> 1) "
        "THEN RAISE EXCEPTION 'refresh family has multiple GitHub users'; "
        "END IF; END; $$"
    )
    op.execute(
        "INSERT INTO auth_refresh_families "
        "(id, github_user_id, expires_at, revoked_at, created_at) "
        "SELECT family_id, min(github_user_id), max(expires_at), min(revoked_at), "
        "min(created_at) FROM auth_refresh_sessions GROUP BY family_id"
    )
    op.create_foreign_key(
        "fk_auth_refresh_sessions_family_id_families",
        "auth_refresh_sessions",
        "auth_refresh_families",
        ["family_id"],
        ["id"],
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_auth_refresh_sessions_family_id_families",
        "auth_refresh_sessions",
        type_="foreignkey",
    )
    op.drop_index("ix_auth_refresh_families_user_id", table_name="auth_refresh_families")
    op.drop_table("auth_refresh_families")
