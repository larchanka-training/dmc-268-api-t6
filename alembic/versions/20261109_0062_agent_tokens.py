"""Repository-scoped, revocable agent credentials.

Revision ID: 20261109_0062
Revises: 20261106_0061
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "20261109_0062"
down_revision = "20261106_0061"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "auth_agent_tokens",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("token_hash", sa.CHAR(64), unique=True, nullable=False),
        sa.Column("github_user_id", sa.BIGINT(), nullable=False),
        sa.Column("repository_id", sa.Uuid(), sa.ForeignKey("repositories.id"), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), sa.ForeignKey("workspaces.id"), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
    )
    op.create_index("ix_auth_agent_tokens_user_id", "auth_agent_tokens", ["github_user_id"])
    op.create_index("ix_auth_agent_tokens_repository_id", "auth_agent_tokens", ["repository_id"])
    op.create_table(
        "auth_agent_device_flows",
        sa.Column("code_hash", sa.CHAR(64), primary_key=True),
        sa.Column("repository", sa.String(512), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("interval", sa.Integer(), nullable=False),
        sa.Column("next_poll_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index(
        "ix_auth_agent_device_flows_expires_at", "auth_agent_device_flows", ["expires_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_auth_agent_device_flows_expires_at", table_name="auth_agent_device_flows")
    op.drop_table("auth_agent_device_flows")
    op.drop_index("ix_auth_agent_tokens_repository_id", table_name="auth_agent_tokens")
    op.drop_index("ix_auth_agent_tokens_user_id", table_name="auth_agent_tokens")
    op.drop_table("auth_agent_tokens")
