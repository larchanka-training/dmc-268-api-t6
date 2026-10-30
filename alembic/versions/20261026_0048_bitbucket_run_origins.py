"""Pin each Bitbucket run to its original repository binding (#333).

Revision ID: 20261026_0048
Revises: 20261026_0047
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "20261026_0048"
down_revision = "20261026_0047"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_unique_constraint(
        "uq_bitbucket_repository_identities_origin",
        "bitbucket_repository_identities",
        ["repository_id", "provider_installation_id", "workspace_id", "repository_uuid"],
    )
    op.create_table(
        "bitbucket_run_origins",
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("provider_installation_id", sa.Uuid(), nullable=False),
        sa.Column("repository_id", sa.Uuid(), nullable=False),
        sa.Column("repository_uuid", sa.Uuid(), nullable=False),
        sa.Column("binding_generation", sa.BigInteger(), nullable=False),
        sa.Column("credential_generation", sa.BigInteger(), nullable=False),
        sa.Column("pr_number", sa.Integer(), nullable=False),
        sa.Column("head_sha", sa.String(64), nullable=False),
        sa.PrimaryKeyConstraint("run_id"),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["provider_installation_id", "workspace_id"],
            [
                "bitbucket_installation_identities.provider_installation_id",
                "bitbucket_installation_identities.workspace_id",
            ],
        ),
        sa.ForeignKeyConstraint(
            ["repository_id", "provider_installation_id", "workspace_id", "repository_uuid"],
            [
                "bitbucket_repository_identities.repository_id",
                "bitbucket_repository_identities.provider_installation_id",
                "bitbucket_repository_identities.workspace_id",
                "bitbucket_repository_identities.repository_uuid",
            ],
        ),
        sa.CheckConstraint(
            "binding_generation > 0", name="ck_bitbucket_run_origins_binding_generation"
        ),
        sa.CheckConstraint(
            "credential_generation > 0", name="ck_bitbucket_run_origins_credential_generation"
        ),
        sa.CheckConstraint("pr_number > 0", name="ck_bitbucket_run_origins_pr_number"),
        sa.CheckConstraint(
            "length(head_sha) BETWEEN 40 AND 64", name="ck_bitbucket_run_origins_head_sha"
        ),
    )


def downgrade() -> None:
    op.execute("LOCK TABLE bitbucket_run_origins IN ACCESS EXCLUSIVE MODE")
    if op.get_bind().scalar(sa.text("SELECT EXISTS (SELECT 1 FROM bitbucket_run_origins)")):
        raise RuntimeError("Bitbucket run origins must be removed before downgrade")
    op.drop_table("bitbucket_run_origins")
    op.drop_constraint(
        "uq_bitbucket_repository_identities_origin",
        "bitbucket_repository_identities",
        type_="unique",
    )
