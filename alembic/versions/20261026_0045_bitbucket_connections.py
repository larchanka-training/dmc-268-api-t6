"""Store encrypted Bitbucket credential generations (#333).

Revision ID: 20261026_0045
Revises: 20261026_0044
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "20261026_0045"
down_revision = "20261026_0044"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "bitbucket_connections",
        sa.Column("provider_installation_id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column(
            "credential_generation", sa.BigInteger(), server_default=sa.text("1"), nullable=False
        ),
        sa.Column("token_ciphertext", sa.LargeBinary(), nullable=True),
        sa.Column("token_key_id", sa.String(64), nullable=True),
        sa.Column("intake_enabled", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("provider_installation_id"),
        sa.ForeignKeyConstraint(
            ["provider_installation_id", "workspace_id"],
            [
                "bitbucket_installation_identities.provider_installation_id",
                "bitbucket_installation_identities.workspace_id",
            ],
        ),
        sa.CheckConstraint("credential_generation > 0", name="ck_bitbucket_connections_generation"),
        sa.CheckConstraint(
            "(token_ciphertext IS NOT NULL AND octet_length(token_ciphertext) > 0 AND "
            "token_key_id IS NOT NULL AND length(trim(token_key_id)) > 0 "
            "AND revoked_at IS NULL) OR "
            "(token_ciphertext IS NULL AND token_key_id IS NULL AND revoked_at IS NOT NULL)",
            name="ck_bitbucket_connections_token_pair",
        ),
        sa.CheckConstraint(
            "NOT intake_enabled OR (revoked_at IS NULL AND token_ciphertext IS NOT NULL)",
            name="ck_bitbucket_connections_enabled",
        ),
    )


def downgrade() -> None:
    op.execute("LOCK TABLE bitbucket_connections IN ACCESS EXCLUSIVE MODE")
    if op.get_bind().scalar(sa.text("SELECT EXISTS (SELECT 1 FROM bitbucket_connections)")):
        raise RuntimeError("Bitbucket connections must be removed before downgrade")
    op.drop_table("bitbucket_connections")
