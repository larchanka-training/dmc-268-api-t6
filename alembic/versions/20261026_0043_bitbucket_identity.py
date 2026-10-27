"""Persist immutable Bitbucket workspace and repository UUID mappings (#333).

Revision ID: 20261026_0043
Revises: 20261026_0042
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "20261026_0043"
down_revision = "20261026_0042"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Existing GitHub/GitLab BIGINT IDs stay unchanged. Bitbucket gets database-
    # allocated surrogate IDs while these tables retain its authoritative UUIDs.
    op.execute(
        "CREATE SEQUENCE bitbucket_installation_external_id_seq AS BIGINT "
        "MINVALUE 1 MAXVALUE 9223372036854775807 START 1 NO CYCLE"
    )
    op.execute(
        "CREATE SEQUENCE bitbucket_repository_external_id_seq AS BIGINT "
        "MINVALUE 1 MAXVALUE 9223372036854775807 START 1 NO CYCLE"
    )
    op.create_table(
        "bitbucket_installation_identities",
        sa.Column("provider_installation_id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("provider", sa.String(50), nullable=False, server_default="bitbucket"),
        sa.Column("bitbucket_workspace_uuid", sa.Uuid(), nullable=False),
        sa.PrimaryKeyConstraint("provider_installation_id"),
        sa.UniqueConstraint(
            "provider_installation_id", "workspace_id", name="uq_bitbucket_installation_owner"
        ),
        sa.UniqueConstraint(
            "workspace_id", "bitbucket_workspace_uuid", name="uq_bitbucket_workspace_identity"
        ),
        sa.CheckConstraint("provider = 'bitbucket'", name="ck_bitbucket_installation_provider"),
        sa.ForeignKeyConstraint(
            ["provider_installation_id", "workspace_id", "provider"],
            [
                "provider_installations.id",
                "provider_installations.workspace_id",
                "provider_installations.provider",
            ],
        ),
    )
    op.create_table(
        "bitbucket_repository_identities",
        sa.Column("repository_id", sa.Uuid(), nullable=False),
        sa.Column("provider_installation_id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("repository_external_id", sa.BigInteger(), nullable=False),
        sa.Column("repository_uuid", sa.Uuid(), nullable=False),
        sa.PrimaryKeyConstraint("repository_id"),
        sa.UniqueConstraint(
            "workspace_id", "repository_uuid", name="uq_bitbucket_workspace_repository_uuid"
        ),
        sa.ForeignKeyConstraint(
            ["provider_installation_id", "workspace_id"],
            [
                "bitbucket_installation_identities.provider_installation_id",
                "bitbucket_installation_identities.workspace_id",
            ],
        ),
        sa.ForeignKeyConstraint(
            ["repository_id", "provider_installation_id", "repository_external_id"],
            [
                "repositories.id",
                "repositories.provider_installation_id",
                "repositories.external_id",
            ],
        ),
    )
    op.execute("""
        CREATE FUNCTION guard_bitbucket_identity() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'Bitbucket UUID identity is immutable' USING ERRCODE='23514';
        END $$
    """)
    for table in ("bitbucket_installation_identities", "bitbucket_repository_identities"):
        op.execute(
            f"CREATE TRIGGER trg_{table}_immutable BEFORE UPDATE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION guard_bitbucket_identity()"
        )


def downgrade() -> None:
    # Exclude concurrent inserts between the safety check and destructive DDL.
    op.execute(
        "LOCK TABLE bitbucket_repository_identities, "
        "bitbucket_installation_identities, provider_installations "
        "IN ACCESS EXCLUSIVE MODE"
    )
    bound = op.get_bind().scalar(
        sa.text(
            "SELECT EXISTS (SELECT 1 FROM bitbucket_installation_identities) OR "
            "EXISTS (SELECT 1 FROM bitbucket_repository_identities) OR "
            "EXISTS (SELECT 1 FROM provider_installations WHERE provider='bitbucket')"
        )
    )
    if bound:
        raise RuntimeError("Bitbucket identity must be removed before downgrade")
    op.drop_table("bitbucket_repository_identities")
    op.drop_table("bitbucket_installation_identities")
    op.execute("DROP FUNCTION guard_bitbucket_identity()")
    op.execute("DROP SEQUENCE bitbucket_repository_external_id_seq")
    op.execute("DROP SEQUENCE bitbucket_installation_external_id_seq")
