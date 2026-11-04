"""Index available repositories for scoped Bitbucket binding pages (#360).

Revision ID: 20261103_0053
Revises: 20261027_0052
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "20261103_0053"
down_revision = "20261027_0052"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("repositories", sa.Column("workspace_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        "fk_repositories_workspace_id", "repositories", "workspaces", ["workspace_id"], ["id"]
    )
    op.execute(
        sa.text("""
        UPDATE repositories AS repository
        SET workspace_id = identity.workspace_id
        FROM bitbucket_repository_identities AS identity
        WHERE identity.repository_id = repository.id
    """)
    )
    op.create_index(
        "ix_repositories_available_installation_name_id",
        "repositories",
        ["provider_installation_id", "full_name", "id"],
        postgresql_where=sa.text("installation_available IS true"),
    )
    op.create_index(
        "ix_repositories_available_workspace_name_id",
        "repositories",
        ["workspace_id", "full_name", "id"],
        postgresql_where=sa.text("installation_available IS true AND workspace_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_repositories_available_workspace_name_id", table_name="repositories")
    op.drop_index("ix_repositories_available_installation_name_id", table_name="repositories")
    op.drop_constraint("fk_repositories_workspace_id", "repositories", type_="foreignkey")
    op.drop_column("repositories", "workspace_id")
