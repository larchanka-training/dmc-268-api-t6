"""Persist GitHub user profiles and hashed local refresh sessions.

Revision ID: 20260928_0017
Revises: 20260928_0016
Create Date: 2026-09-28
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "20260928_0017"
down_revision = "20260928_0016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "github_user_profiles",
        sa.Column("id", sa.BIGINT(), primary_key=True),
        sa.Column("login", sa.String(255), nullable=False),
        sa.Column("name", sa.String(255)),
        sa.Column("avatar_url", sa.String(2048)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("id > 0", name="ck_github_user_profiles_id_positive"),
    )
    op.create_table(
        "auth_refresh_sessions",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("family_id", sa.Uuid(), nullable=False),
        sa.Column(
            "github_user_id", sa.BIGINT(), sa.ForeignKey("github_user_profiles.id"), nullable=False
        ),
        sa.Column("token_hash", sa.CHAR(64), unique=True, nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("rotated_at", sa.DateTime(timezone=True)),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
    )
    op.create_index("ix_auth_refresh_sessions_family_id", "auth_refresh_sessions", ["family_id"])
    op.create_index("ix_auth_refresh_sessions_user_id", "auth_refresh_sessions", ["github_user_id"])


def downgrade() -> None:
    op.drop_index("ix_auth_refresh_sessions_user_id", table_name="auth_refresh_sessions")
    op.drop_index("ix_auth_refresh_sessions_family_id", table_name="auth_refresh_sessions")
    op.drop_table("auth_refresh_sessions")
    op.drop_table("github_user_profiles")
