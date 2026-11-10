"""Store rules playground simulations: pasted code reviewed under pasted rules (#311).

Revision ID: 20261110_0062
Revises: 20261106_0061
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "20261110_0062"
down_revision = "20261106_0061"
branch_labels = None
depends_on = None

_MODES = ("code", "diff")
_STATUSES = ("queued", "running", "succeeded", "failed")


def upgrade() -> None:
    bind = op.get_bind()
    postgresql.ENUM(*_MODES, name="rules_simulation_mode").create(bind, checkfirst=False)
    postgresql.ENUM(*_STATUSES, name="rules_simulation_status").create(bind, checkfirst=False)
    op.create_table(
        "rules_simulations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("repository_id", sa.Uuid(), nullable=False),
        sa.Column("github_user_id", sa.BIGINT(), nullable=False),
        sa.Column(
            "mode",
            postgresql.ENUM(*_MODES, name="rules_simulation_mode", create_type=False),
            nullable=False,
        ),
        sa.Column("path", sa.TEXT(), nullable=False),
        sa.Column("content", sa.TEXT(), nullable=False),
        sa.Column("rules_md", sa.TEXT(), nullable=False),
        sa.Column(
            "status",
            postgresql.ENUM(*_STATUSES, name="rules_simulation_status", create_type=False),
            server_default="queued",
            nullable=False,
        ),
        sa.Column("error_code", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["repository_id"], ["repositories.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["github_user_id"], ["github_user_profiles.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(
            "(status IN ('succeeded', 'failed')) = (finished_at IS NOT NULL)",
            name="ck_rules_simulations_finished",
        ),
        sa.CheckConstraint(
            "(status = 'failed') = (error_code IS NOT NULL)",
            name="ck_rules_simulations_error_code",
        ),
        sa.CheckConstraint(
            "octet_length(content) <= 65536 AND octet_length(rules_md) <= 65536",
            name="ck_rules_simulations_input_size",
        ),
    )
    op.create_index(
        "uq_rules_simulations_one_active_per_user",
        "rules_simulations",
        ["github_user_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('queued', 'running')"),
    )
    op.create_index("ix_rules_simulations_repository_id", "rules_simulations", ["repository_id"])


def downgrade() -> None:
    op.drop_index("ix_rules_simulations_repository_id", table_name="rules_simulations")
    op.drop_index("uq_rules_simulations_one_active_per_user", table_name="rules_simulations")
    op.drop_table("rules_simulations")
    bind = op.get_bind()
    postgresql.ENUM(name="rules_simulation_status").drop(bind, checkfirst=False)
    postgresql.ENUM(name="rules_simulation_mode").drop(bind, checkfirst=False)
