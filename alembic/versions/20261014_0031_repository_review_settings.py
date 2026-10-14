"""Store the target-branch filter and the severity threshold of a repository (#149).

Revision ID: 20261014_0031
Revises: 20261010_0030
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "20261014_0031"
down_revision = "20261010_0030"
branch_labels = None
depends_on = None

_SEVERITY_THRESHOLDS = ("all", "warning", "critical")


def upgrade() -> None:
    postgresql.ENUM(*_SEVERITY_THRESHOLDS, name="severity_threshold").create(
        op.get_bind(), checkfirst=False
    )
    op.add_column(
        "repositories",
        sa.Column(
            "branch_filter",
            postgresql.ARRAY(sa.String(length=255)),
            server_default=sa.text("'{}'"),
            nullable=False,
        ),
    )
    op.add_column(
        "repositories",
        sa.Column(
            "severity_threshold",
            postgresql.ENUM(*_SEVERITY_THRESHOLDS, name="severity_threshold", create_type=False),
            server_default="all",
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_column("repositories", "severity_threshold")
    op.drop_column("repositories", "branch_filter")
    postgresql.ENUM(*_SEVERITY_THRESHOLDS, name="severity_threshold").drop(
        op.get_bind(), checkfirst=False
    )
