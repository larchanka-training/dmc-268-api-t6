"""Track authenticated GitHub user access to installation Workspaces.

Revision ID: 20260928_0013
Revises: 20260928_0012
Create Date: 2026-09-28
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "20260928_0013"
down_revision = "20260928_0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "github_user_workspace_access",
        sa.Column("github_user_id", sa.BIGINT(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "github_user_id > 0", name="ck_github_user_workspace_access_user_positive"
        ),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"]),
        sa.PrimaryKeyConstraint("github_user_id", "workspace_id"),
    )
    op.create_index(
        "ix_github_user_workspace_access_workspace_id",
        "github_user_workspace_access",
        ["workspace_id"],
    )
    op.create_table(
        "github_user_repository_access",
        sa.Column("github_user_id", sa.BIGINT(), nullable=False),
        sa.Column("provider_installation_id", sa.Uuid(), nullable=False),
        sa.Column("repository_external_id", sa.BIGINT(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "github_user_id > 0", name="ck_github_user_repository_access_user_positive"
        ),
        sa.CheckConstraint(
            "repository_external_id > 0",
            name="ck_github_user_repository_access_repository_positive",
        ),
        sa.ForeignKeyConstraint(["provider_installation_id"], ["provider_installations.id"]),
        sa.PrimaryKeyConstraint(
            "github_user_id", "provider_installation_id", "repository_external_id"
        ),
    )
    op.create_index(
        "ix_github_user_repository_access_installation_repository",
        "github_user_repository_access",
        ["provider_installation_id", "repository_external_id"],
    )
    op.create_table(
        "github_user_installation_sync",
        sa.Column("github_user_id", sa.BIGINT(), nullable=False),
        sa.Column("reserved_generation", sa.BIGINT(), nullable=False),
        sa.Column("applied_generation", sa.BIGINT(), nullable=False),
        sa.CheckConstraint(
            "github_user_id > 0", name="ck_github_user_installation_sync_user_positive"
        ),
        sa.CheckConstraint(
            "reserved_generation >= applied_generation AND applied_generation >= 0",
            name="ck_github_user_installation_sync_generation_order",
        ),
        sa.PrimaryKeyConstraint("github_user_id"),
    )


def downgrade() -> None:
    op.drop_table("github_user_installation_sync")
    op.drop_index(
        "ix_github_user_repository_access_installation_repository",
        table_name="github_user_repository_access",
    )
    op.drop_table("github_user_repository_access")
    op.drop_index(
        "ix_github_user_workspace_access_workspace_id",
        table_name="github_user_workspace_access",
    )
    op.drop_table("github_user_workspace_access")
