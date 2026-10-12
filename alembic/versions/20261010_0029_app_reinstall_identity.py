"""Preserve verified account evidence and App lifecycle state.

Revision ID: 20261010_0029
Revises: 20261007_0028
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "20261010_0029"
down_revision = "20261007_0028"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Legacy login metadata cannot establish numeric account identity.
    op.add_column(
        "provider_installations", sa.Column("account_external_id", sa.BigInteger(), nullable=True)
    )
    op.add_column(
        "provider_installations", sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.create_check_constraint(
        "ck_provider_installations_account_positive",
        "provider_installations",
        "account_external_id > 0",
    )
    op.create_index(
        "ix_provider_installations_provider_account",
        "provider_installations",
        ["provider", "account_external_id"],
    )
    op.add_column(
        "repositories",
        sa.Column("installation_available", sa.Boolean(), nullable=False, server_default=sa.true()),
    )
    # Only the whole-installation tombstone proves deletion, not a selected-repo removal.
    op.execute(
        sa.text("""
        UPDATE provider_installations AS installation
        SET deleted_at = revocation.revoked_at
        FROM github_installation_access_revocations AS revocation
        WHERE installation.id = revocation.provider_installation_id
          AND installation.provider = 'github'
          AND revocation.repository_external_id = 0
    """)
    )
    # Keep the user's ambiguous legacy enabled preference exactly as stored.
    # Legacy enabled=true or a fresh persisted grant proves a later positive projection;
    # a whole-installation deletion remains retired regardless of grants.
    op.execute(
        sa.text("""
        UPDATE repositories AS repository
        SET installation_available = false
        FROM provider_installations AS installation
        WHERE repository.provider_installation_id = installation.id
          AND installation.provider = 'github'
          AND (
              installation.deleted_at IS NOT NULL
              OR EXISTS (
                  SELECT 1 FROM github_installation_access_revocations AS revocation
                  WHERE revocation.provider_installation_id = installation.id
                    AND revocation.repository_external_id = repository.external_id
                    AND repository.enabled = false
                    AND NOT EXISTS (
                        SELECT 1 FROM github_user_repository_access AS access
                        WHERE access.provider_installation_id = installation.id
                          AND access.repository_external_id = repository.external_id
                          AND access.created_at > revocation.revoked_at
                    )
              )
          )
    """)
    )


def downgrade() -> None:
    # Old writers only understand enabled; never revive an unavailable repository.
    op.execute(sa.text("UPDATE repositories SET enabled = enabled AND installation_available"))
    op.drop_column("repositories", "installation_available")
    op.drop_index("ix_provider_installations_provider_account", table_name="provider_installations")
    op.drop_constraint(
        "ck_provider_installations_account_positive", "provider_installations", type_="check"
    )
    op.drop_column("provider_installations", "deleted_at")
    op.drop_column("provider_installations", "account_external_id")
