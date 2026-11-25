"""Retire the duplicate live GitHub installations recorded for one account.

Revision ID: 20261125_0069
Revises: 20261114_0068

The three installations 169030002, 172223009 and 172686059 are the rows the
switcher still shows as separate "GitHub: axyi" workspaces (#511). Rebind only
adopts a source that is already deleted and shares account_external_id, so a
reinstall whose previous row was still live created another workspace. GitHub
installation ids increase; the greatest id that is still live is the one the
account still has. Older live rows in this set are retired the way
installation.deleted would have retired them. A database without these rows
is unchanged. 173181621 (larchanka-training) is not in the set.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "20261125_0069"
down_revision = "20261114_0068"
branch_labels = None
depends_on = None

RETIRE_DUPLICATE_INSTALLATIONS = sa.text(
    """
    DO $$
    DECLARE
        retiring uuid[];
    BEGIN
        WITH live AS (
            SELECT id, external_id
            FROM provider_installations
            WHERE provider = 'github'
              AND external_id IN (169030002, 172223009, 172686059)
              AND deleted_at IS NULL
        ),
        keeper AS (
            SELECT id
            FROM live
            ORDER BY external_id DESC
            LIMIT 1
        )
        SELECT COALESCE(array_agg(live.id), ARRAY[]::uuid[])
        INTO retiring
        FROM live
        WHERE (SELECT count(*) FROM live) >= 2
          AND live.id IS DISTINCT FROM (SELECT keeper.id FROM keeper);

        UPDATE provider_installations
        SET deleted_at = clock_timestamp()
        WHERE id = ANY(retiring);

        UPDATE repositories
        SET installation_available = false
        WHERE provider_installation_id = ANY(retiring);

        DELETE FROM github_user_repository_access
        WHERE provider_installation_id = ANY(retiring);
    END $$;
    """
)


def upgrade() -> None:
    op.execute(RETIRE_DUPLICATE_INSTALLATIONS)


def downgrade() -> None:
    # deleted_at is permanent. Downgrade does not revive the retired rows,
    # their repository availability, or their grants.
    pass
