"""Pin the original installation for historical and future Run provider work.

Revision ID: 20261010_0030
Revises: 20261010_0029
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "20261010_0030"
down_revision = "20261010_0029"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("runs", sa.Column("origin_provider_installation_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        "runs_origin_provider_installation_id_fkey",
        "runs",
        "provider_installations",
        ["origin_provider_installation_id"],
        ["id"],
    )
    # The migration precedes repository rebinding; this is the original association.
    # Do not infer it at runtime after ownership changes or rewrite historical payloads.
    op.execute(
        sa.text("""
        UPDATE runs AS run
        SET origin_provider_installation_id = repository.provider_installation_id
        FROM code_changes AS change, repositories AS repository
        WHERE run.code_change_id = change.id
          AND change.repository_id = repository.id
          AND run.origin_provider_installation_id IS NULL
    """)
    )


def downgrade() -> None:
    op.drop_constraint("runs_origin_provider_installation_id_fkey", "runs", type_="foreignkey")
    op.drop_column("runs", "origin_provider_installation_id")
