"""Store isolated encrypted Bitbucket repository hook secrets (#333).

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
        "bitbucket_repository_webhook_secrets",
        sa.Column("repository_id", sa.Uuid(), nullable=False),
        sa.Column("generation", sa.BigInteger(), server_default=sa.text("1"), nullable=False),
        sa.Column(
            "binding_generation", sa.BigInteger(), server_default=sa.text("1"), nullable=False
        ),
        sa.Column("secret_ciphertext", sa.LargeBinary(), nullable=True),
        sa.Column("secret_key_id", sa.String(64), nullable=True),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("repository_id"),
        sa.ForeignKeyConstraint(
            ["repository_id"], ["bitbucket_repository_identities.repository_id"]
        ),
        sa.CheckConstraint("generation > 0", name="ck_bitbucket_repository_webhook_generation"),
        sa.CheckConstraint("binding_generation > 0", name="ck_bitbucket_binding_generation"),
        sa.CheckConstraint(
            "(secret_ciphertext IS NOT NULL AND octet_length(secret_ciphertext) > 0 "
            "AND secret_key_id IS NOT NULL AND length(trim(secret_key_id)) > 0 "
            "AND revoked_at IS NULL) OR "
            "(secret_ciphertext IS NULL AND secret_key_id IS NULL AND revoked_at IS NULL) OR "
            "(secret_ciphertext IS NULL AND secret_key_id IS NULL AND revoked_at IS NOT NULL)",
            name="ck_bitbucket_repository_webhook_secret_pair",
        ),
        sa.CheckConstraint(
            "confirmed_at IS NULL OR (secret_ciphertext IS NOT NULL AND revoked_at IS NULL)",
            name="ck_bitbucket_repository_webhook_confirmation",
        ),
    )


def downgrade() -> None:
    op.execute("LOCK TABLE bitbucket_repository_webhook_secrets IN ACCESS EXCLUSIVE MODE")
    if op.get_bind().scalar(
        sa.text("SELECT EXISTS (SELECT 1 FROM bitbucket_repository_webhook_secrets)")
    ):
        raise RuntimeError("Bitbucket repository hook secrets must be removed before downgrade")
    op.drop_table("bitbucket_repository_webhook_secrets")
