"""Persist installation-owned GitLab connections and current reservations (#169).

Revision ID: 20261026_0042
Revises: 20261026_0041
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "20261026_0042"
down_revision = "20261026_0041"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "CREATE SEQUENCE gitlab_installation_external_id_seq AS BIGINT MINVALUE 1"
        " MAXVALUE 9223372036854775807 START 1 NO CYCLE"
    )
    op.execute("""
        SELECT setval('gitlab_installation_external_id_seq',
            GREATEST(COALESCE((SELECT max(external_id) FROM provider_installations
                WHERE provider='gitlab' AND external_id > 0), 0), 1),
            EXISTS (SELECT 1 FROM provider_installations
                WHERE provider='gitlab' AND external_id > 0))
    """)
    op.create_table(
        "gitlab_connections",
        sa.Column("provider_installation_id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("provider", sa.String(50), nullable=False, server_default="gitlab"),
        sa.Column("instance_origin", sa.Text(), nullable=False),
        sa.Column(
            "credential_generation", sa.BigInteger(), nullable=False, server_default=sa.text("1")
        ),
        sa.Column("token_ciphertext", sa.LargeBinary(), nullable=True),
        sa.Column("credential_key_id", sa.String(64), nullable=True),
        sa.Column("webhook_secret_digest", sa.CHAR(64), nullable=False),
        sa.Column("intake_enabled", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.PrimaryKeyConstraint("provider_installation_id"),
        sa.ForeignKeyConstraint(
            ["provider_installation_id", "workspace_id", "provider"],
            [
                "provider_installations.id",
                "provider_installations.workspace_id",
                "provider_installations.provider",
            ],
        ),
        sa.UniqueConstraint(
            "provider_installation_id", "instance_origin", name="uq_gitlab_connections_origin"
        ),
        sa.CheckConstraint("provider = 'gitlab'", name="ck_gitlab_connections_provider"),
        sa.CheckConstraint("credential_generation > 0", name="ck_gitlab_connections_generation"),
        sa.CheckConstraint(
            "webhook_secret_digest ~ '^[0-9a-f]{64}$'", name="ck_gitlab_connections_webhook_digest"
        ),
        sa.CheckConstraint(
            (
                "instance_origin ~ "
                "'^https://([a-z0-9][a-z0-9.-]*|\\[[0-9a-f:]+\\])(:[1-9][0-9]{0,4})?$' AND "
                "instance_origin !~ ':[4]43$'"
            ),
            name="ck_gitlab_connections_origin",
        ),
        sa.CheckConstraint(
            (
                "(token_ciphertext IS NOT NULL AND octet_length(token_ciphertext) > 0 AND"
                " credential_key_id IS NOT NULL AND length(trim(credential_key_id)) > 0) "
                "OR (token_ciphertext IS NULL AND credential_key_id IS NULL AND "
                "revoked_at IS NOT NULL)"
            ),
            name="ck_gitlab_connections_credential_pair",
        ),
        sa.CheckConstraint(
            (
                "NOT intake_enabled OR (revoked_at IS NULL AND token_ciphertext IS NOT "
                "NULL AND credential_key_id IS NOT NULL)"
            ),
            name="ck_gitlab_connections_enabled",
        ),
    )
    op.execute("""
        CREATE FUNCTION guard_gitlab_connection_identity() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF ROW(NEW.provider_installation_id, NEW.workspace_id, NEW.provider,
                NEW.instance_origin)
                IS DISTINCT FROM ROW(OLD.provider_installation_id, OLD.workspace_id, OLD.provider,
                    OLD.instance_origin)
                OR (OLD.revoked_at IS NOT NULL AND NEW.revoked_at IS DISTINCT FROM OLD.revoked_at)
            THEN RAISE EXCEPTION 'GitLab connection identity and revocation are immutable'
                USING ERRCODE='23514';
            END IF;
            RETURN NEW;
        END $$
    """)
    op.execute(
        "CREATE TRIGGER trg_gitlab_connection_identity BEFORE UPDATE ON "
        "gitlab_connections FOR EACH ROW EXECUTE FUNCTION "
        "guard_gitlab_connection_identity()"
    )

    op.create_table(
        "gitlab_project_bindings",
        sa.Column("repository_id", sa.Uuid(), nullable=False),
        sa.Column("provider_installation_id", sa.Uuid(), nullable=False),
        sa.Column("instance_origin", sa.Text(), nullable=False),
        sa.Column("project_external_id", sa.BigInteger(), nullable=False),
        sa.PrimaryKeyConstraint("repository_id"),
        sa.ForeignKeyConstraint(
            ["provider_installation_id", "instance_origin"],
            ["gitlab_connections.provider_installation_id", "gitlab_connections.instance_origin"],
        ),
        sa.ForeignKeyConstraint(
            ["repository_id", "provider_installation_id", "project_external_id"],
            [
                "repositories.id",
                "repositories.provider_installation_id",
                "repositories.external_id",
            ],
        ),
        sa.UniqueConstraint(
            "instance_origin", "project_external_id", name="uq_gitlab_project_bindings_project"
        ),
        sa.CheckConstraint(
            "project_external_id > 0", name="ck_gitlab_project_bindings_project_positive"
        ),
    )

    op.add_column(
        "webhook_events",
        sa.Column("provider", sa.String(50), nullable=False, server_default="github"),
    )
    op.add_column("webhook_events", sa.Column("gitlab_connection_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        "webhook_events_gitlab_connection_id_fkey",
        "webhook_events",
        "gitlab_connections",
        ["gitlab_connection_id"],
        ["provider_installation_id"],
    )
    op.create_check_constraint(
        "ck_webhook_events_provider", "webhook_events", "provider IN ('github', 'gitlab')"
    )
    op.create_check_constraint(
        "ck_webhook_events_gitlab_origin",
        "webhook_events",
        (
            "(provider = 'github' AND gitlab_connection_id IS NULL) OR (provider = "
            "'gitlab' AND gitlab_connection_id IS NOT NULL AND "
            "provider_installation_id IS NOT NULL AND gitlab_connection_id = "
            "provider_installation_id)"
        ),
    )
    op.execute("""
        CREATE FUNCTION guard_webhook_event_origin() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF ROW(NEW.provider, NEW.gitlab_connection_id, NEW.provider_installation_id)
                IS DISTINCT FROM ROW(OLD.provider, OLD.gitlab_connection_id,
                    OLD.provider_installation_id)
            THEN RAISE EXCEPTION 'Webhook receipt origin is immutable' USING ERRCODE='23514';
            END IF;
            RETURN NEW;
        END $$
    """)
    op.execute(
        "CREATE TRIGGER trg_webhook_event_origin BEFORE UPDATE ON webhook_events "
        "FOR EACH ROW EXECUTE FUNCTION guard_webhook_event_origin()"
    )


def downgrade() -> None:
    # Exclude concurrent inserts between the safety check and destructive DDL.
    op.execute("LOCK TABLE gitlab_connections, webhook_events IN ACCESS EXCLUSIVE MODE")
    if op.get_bind().scalar(
        sa.text(
            "SELECT EXISTS (SELECT 1 FROM gitlab_connections) OR EXISTS (SELECT 1 "
            "FROM webhook_events WHERE provider='gitlab')"
        )
    ):
        raise RuntimeError("remove GitLab configuration and receipts explicitly before downgrade")
    op.execute("DROP TRIGGER trg_webhook_event_origin ON webhook_events")
    op.execute("DROP FUNCTION guard_webhook_event_origin()")
    op.drop_constraint("ck_webhook_events_gitlab_origin", "webhook_events", type_="check")
    op.drop_constraint("ck_webhook_events_provider", "webhook_events", type_="check")
    op.drop_constraint(
        "webhook_events_gitlab_connection_id_fkey", "webhook_events", type_="foreignkey"
    )
    op.drop_column("webhook_events", "gitlab_connection_id")
    op.drop_column("webhook_events", "provider")
    op.drop_table("gitlab_project_bindings")
    op.drop_table("gitlab_connections")
    op.execute("DROP FUNCTION guard_gitlab_connection_identity()")
    op.execute("DROP SEQUENCE gitlab_installation_external_id_seq")
