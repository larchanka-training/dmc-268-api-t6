"""Move repositories and waiting runs off the deep engine until phase 3.

``deep`` (SandboxEngine, SD §13) has no worker in the MVP: a repository set to it
queued runs that nobody consumed. Repositories and still queued runs switch to
``fast``; finished runs keep their history.

Revision ID: 20261005_0025
Revises: 20261005_0024
Create Date: 2026-10-05
"""

from __future__ import annotations

from alembic import op

revision = "20261005_0025"
down_revision = "20261005_0024"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("UPDATE repositories SET default_engine = 'fast' WHERE default_engine = 'deep'")
    op.execute("UPDATE runs SET engine = 'fast' WHERE engine = 'deep' AND state = 'queued'")


def downgrade() -> None:
    # Data migration: the previous engine of each row is not kept.
    pass
