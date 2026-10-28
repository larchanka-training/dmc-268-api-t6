"""Bind durable webhook receipts to Bitbucket repository origin (#333).

Revision ID: 20261026_0047
Revises: 20261026_0046
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "20261026_0047"
down_revision = "20261026_0046"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("webhook_events", sa.Column("bitbucket_repository_id", sa.Uuid(), nullable=True))
    op.add_column(
        "webhook_events", sa.Column("bitbucket_binding_generation", sa.BigInteger(), nullable=True)
    )
    op.add_column(
        "webhook_events", sa.Column("bitbucket_webhook_generation", sa.BigInteger(), nullable=True)
    )
    op.create_foreign_key(
        "fk_webhook_events_bitbucket_repository_id",
        "webhook_events",
        "bitbucket_repository_identities",
        ["bitbucket_repository_id"],
        ["repository_id"],
    )
    op.drop_constraint("ck_webhook_events_gitlab_origin", "webhook_events", type_="check")
    op.drop_constraint("ck_webhook_events_provider", "webhook_events", type_="check")
    op.create_check_constraint(
        "ck_webhook_events_provider",
        "webhook_events",
        "provider IN ('github', 'gitlab', 'bitbucket')",
    )
    op.create_check_constraint(
        "ck_webhook_events_provider_origin",
        "webhook_events",
        "(provider = 'github' AND gitlab_connection_id IS NULL AND "
        "bitbucket_repository_id IS NULL AND bitbucket_binding_generation IS NULL "
        "AND bitbucket_webhook_generation IS NULL) OR "
        "(provider = 'gitlab' AND gitlab_connection_id IS NOT NULL AND "
        "provider_installation_id IS NOT NULL AND "
        "gitlab_connection_id = provider_installation_id AND "
        "bitbucket_repository_id IS NULL AND bitbucket_binding_generation IS NULL "
        "AND bitbucket_webhook_generation IS NULL) OR "
        "(provider = 'bitbucket' AND gitlab_connection_id IS NULL AND "
        "provider_installation_id IS NOT NULL AND bitbucket_repository_id IS NOT NULL "
        "AND bitbucket_binding_generation IS NOT NULL "
        "AND bitbucket_webhook_generation IS NOT NULL "
        "AND bitbucket_binding_generation > 0 AND bitbucket_webhook_generation > 0)",
    )


def downgrade() -> None:
    op.execute("LOCK TABLE webhook_events IN ACCESS EXCLUSIVE MODE")
    if op.get_bind().scalar(
        sa.text("SELECT EXISTS (SELECT 1 FROM webhook_events WHERE provider='bitbucket')")
    ):
        raise RuntimeError("Bitbucket webhook receipts must be removed before downgrade")
    op.drop_constraint("ck_webhook_events_provider_origin", "webhook_events", type_="check")
    op.drop_constraint("ck_webhook_events_provider", "webhook_events", type_="check")
    op.drop_constraint(
        "fk_webhook_events_bitbucket_repository_id", "webhook_events", type_="foreignkey"
    )
    op.drop_column("webhook_events", "bitbucket_webhook_generation")
    op.drop_column("webhook_events", "bitbucket_binding_generation")
    op.drop_column("webhook_events", "bitbucket_repository_id")
    op.create_check_constraint(
        "ck_webhook_events_provider", "webhook_events", "provider IN ('github', 'gitlab')"
    )
    op.create_check_constraint(
        "ck_webhook_events_gitlab_origin",
        "webhook_events",
        "(provider = 'github' AND gitlab_connection_id IS NULL) OR "
        "(provider = 'gitlab' AND gitlab_connection_id IS NOT NULL AND "
        "provider_installation_id IS NOT NULL AND "
        "gitlab_connection_id = provider_installation_id)",
    )
