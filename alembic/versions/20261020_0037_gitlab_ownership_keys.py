"""Add referenceable installation and project ownership keys (#169).

Revision ID: 20261020_0037
Revises: 20261019_0036
"""

from __future__ import annotations

from alembic import op

revision = "20261020_0037"
down_revision = "20261019_0036"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_unique_constraint(
        "uq_provider_installations_owner",
        "provider_installations",
        ["id", "workspace_id", "provider"],
    )
    op.create_unique_constraint(
        "uq_repositories_owner",
        "repositories",
        ["id", "provider_installation_id", "external_id"],
    )


def downgrade() -> None:
    op.drop_constraint("uq_repositories_owner", "repositories", type_="unique")
    op.drop_constraint("uq_provider_installations_owner", "provider_installations", type_="unique")
