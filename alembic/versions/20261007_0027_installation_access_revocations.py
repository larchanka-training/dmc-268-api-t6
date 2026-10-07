"""Track App revocations across concurrent OAuth snapshots.

Revision ID: 20261007_0027
Revises: 20261006_0026
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "20261007_0027"
down_revision = "20261006_0026"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "github_installation_access_revocations",
        sa.Column(
            "provider_installation_id",
            sa.Uuid(),
            sa.ForeignKey("provider_installations.id"),
            primary_key=True,
        ),
        sa.Column("repository_external_id", sa.BigInteger(), primary_key=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "repository_external_id >= 0",
            name="ck_github_installation_access_revocations_repo_nonnegative",
        ),
    )


def downgrade() -> None:
    op.drop_table("github_installation_access_revocations")
