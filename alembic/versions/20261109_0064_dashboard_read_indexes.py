"""Index the reads of the metrics summary and the run list (#456).

Each index builds in well under a second on a year of history at 100 PRs a day
(docs/test-plan/db-profile-456.md), so a plain CREATE INDEX holds writes only that long.

Revision ID: 20261109_0064
Revises: 20261106_0061
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "20261109_0064"
down_revision = "20261106_0061"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Predicates are written as the reads send them, or the planner cannot use the index.
    op.create_index(
        "ix_findings_published_run_id",
        "findings",
        ["run_id"],
        postgresql_include=["severity", "category"],
        postgresql_where=sa.text("published IS TRUE AND drop_reason IS NULL"),
    )
    op.create_index(
        "ix_usage_events_run_totals",
        "usage_events",
        ["run_id"],
        postgresql_include=["tokens_in", "tokens_out", "cost_usd"],
    )
    op.create_index(
        "ix_run_actions_postprocess_run_index",
        "run_actions",
        ["run_id", sa.text("index DESC")],
        postgresql_where=sa.text("tool = 'review.postprocess'"),
    )
    op.create_index(
        "ix_context_payloads_summary_only_stop",
        "context_payloads",
        ["run_id"],
        postgresql_where=sa.text(
            "(summary ->> 'status') = 'summary_only' AND NOT (summary ? 'reviewable_lines')"
        ),
    )


def downgrade() -> None:
    op.drop_index("ix_context_payloads_summary_only_stop", table_name="context_payloads")
    op.drop_index("ix_run_actions_postprocess_run_index", table_name="run_actions")
    op.drop_index("ix_usage_events_run_totals", table_name="usage_events")
    op.drop_index("ix_findings_published_run_id", table_name="findings")
