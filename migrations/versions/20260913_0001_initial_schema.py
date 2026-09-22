"""Create the initial review-service PostgreSQL schema.

Revision ID: 20260913_0001
Revises: None
Create Date: 2026-09-13

Frozen schema snapshot: never import runtime models into migration revisions.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "20260913_0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    postgresql.ENUM("pending", "paid", "failed", "expired", name="payment_status").create(
        op.get_bind(), checkfirst=False
    )
    postgresql.ENUM("fast", "deep", name="engine").create(op.get_bind(), checkfirst=False)
    postgresql.ENUM("auto", "always", "never", name="wait_for_ci").create(
        op.get_bind(), checkfirst=False
    )
    postgresql.ENUM("comment", "request_changes", name="review_event").create(
        op.get_bind(), checkfirst=False
    )
    postgresql.ENUM("open", "closed", "merged", name="code_change_state").create(
        op.get_bind(), checkfirst=False
    )
    postgresql.ENUM(
        "queued",
        "running",
        "publishing",
        "succeeded",
        "failed",
        "cancelled",
        "skipped",
        name="run_state",
    ).create(op.get_bind(), checkfirst=False)
    postgresql.ENUM("top_up", "usage_debit", "adjustment", name="ledger_kind").create(
        op.get_bind(), checkfirst=False
    )
    op.create_table(
        "prompt_versions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("key", sa.String(length=100), nullable=False),
        sa.Column("version", sa.INTEGER(), nullable=False),
        sa.Column("content", sa.TEXT(), nullable=False),
        sa.Column("checksum", sa.CHAR(length=64), nullable=False),
        sa.Column("is_active", sa.BOOLEAN(), server_default=sa.text("false"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("checksum"),
        sa.UniqueConstraint("key", "version"),
    )
    op.create_table(
        "workspaces",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("daily_budget_usd", sa.Numeric(precision=12, scale=6), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "payments",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("provider", sa.String(length=50), nullable=False),
        sa.Column("provider_checkout_id", sa.String(length=255), nullable=True),
        sa.Column("provider_payment_id", sa.String(length=255), nullable=True),
        sa.Column("amount_usd", sa.Numeric(precision=12, scale=6), nullable=False),
        sa.Column(
            "status",
            postgresql.ENUM(
                "pending", "paid", "failed", "expired", name="payment_status", create_type=False
            ),
            server_default="pending",
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("paid_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "provider_installations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("provider", sa.String(length=50), nullable=False),
        sa.Column("external_id", sa.BIGINT(), nullable=False),
        sa.Column("metadata", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("provider", "external_id"),
    )
    op.create_table(
        "repositories",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("provider_installation_id", sa.Uuid(), nullable=False),
        sa.Column("external_id", sa.BIGINT(), nullable=False),
        sa.Column("full_name", sa.String(length=512), nullable=False),
        sa.Column("default_branch", sa.String(length=255), nullable=False),
        sa.Column("web_url", sa.TEXT(), nullable=False),
        sa.Column("enabled", sa.BOOLEAN(), server_default=sa.text("true"), nullable=False),
        sa.Column(
            "default_engine",
            postgresql.ENUM("fast", "deep", name="engine", create_type=False),
            server_default="fast",
            nullable=False,
        ),
        sa.Column("prompt_version_id", sa.Uuid(), nullable=True),
        sa.Column(
            "wait_for_ci",
            postgresql.ENUM("auto", "always", "never", name="wait_for_ci", create_type=False),
            server_default="auto",
            nullable=False,
        ),
        sa.Column(
            "review_event",
            postgresql.ENUM("comment", "request_changes", name="review_event", create_type=False),
            server_default="comment",
            nullable=False,
        ),
        sa.Column("max_comments", sa.SMALLINT(), server_default=sa.text("10"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("max_comments > 0", name="ck_repositories_max_comments_positive"),
        sa.ForeignKeyConstraint(
            ["prompt_version_id"],
            ["prompt_versions.id"],
        ),
        sa.ForeignKeyConstraint(
            ["provider_installation_id"],
            ["provider_installations.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("provider_installation_id", "external_id"),
    )
    op.create_table(
        "webhook_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("provider_installation_id", sa.Uuid(), nullable=True),
        sa.Column("installation_external_id", sa.BIGINT(), nullable=False),
        sa.Column("delivery_id", sa.String(length=255), nullable=False),
        sa.Column("event", sa.String(length=100), nullable=False),
        sa.Column("action", sa.String(length=100), nullable=True),
        sa.Column("payload_s3_ref", sa.TEXT(), nullable=False),
        sa.Column(
            "received_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["provider_installation_id"],
            ["provider_installations.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("delivery_id"),
    )
    op.create_table(
        "code_changes",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("repository_id", sa.Uuid(), nullable=False),
        sa.Column("external_id", sa.BIGINT(), nullable=False),
        sa.Column("external_number", sa.INTEGER(), nullable=False),
        sa.Column("title", sa.String(length=500), nullable=False),
        sa.Column("description", sa.TEXT(), nullable=True),
        sa.Column("source_branch", sa.String(length=255), nullable=False),
        sa.Column("target_branch", sa.String(length=255), nullable=False),
        sa.Column("base_sha", sa.String(length=64), nullable=False),
        sa.Column("head_sha", sa.String(length=64), nullable=False),
        sa.Column(
            "state",
            postgresql.ENUM(
                "open", "closed", "merged", name="code_change_state", create_type=False
            ),
            nullable=False,
        ),
        sa.Column(
            "reviewer_requested", sa.BOOLEAN(), server_default=sa.text("false"), nullable=False
        ),
        sa.Column(
            "ci_status",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'"),
            nullable=False,
        ),
        sa.Column("web_url", sa.TEXT(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["repository_id"],
            ["repositories.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("repository_id", "external_id"),
        sa.UniqueConstraint("repository_id", "external_number"),
    )
    op.create_table(
        "repo_conventions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("repository_id", sa.Uuid(), nullable=False),
        sa.Column("agents_md_sha", sa.String(length=64), nullable=True),
        sa.Column("prompt_version_id", sa.Uuid(), nullable=False),
        sa.Column("agents_md", sa.TEXT(), nullable=True),
        sa.Column("key_patterns", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("recommendations", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("languages", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["prompt_version_id"],
            ["prompt_versions.id"],
        ),
        sa.ForeignKeyConstraint(
            ["repository_id"],
            ["repositories.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "repository_id",
            "agents_md_sha",
            "prompt_version_id",
            name="uq_repo_conventions_snapshot",
            postgresql_nulls_not_distinct=True,
        ),
    )
    op.create_table(
        "rule_versions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("repository_id", sa.Uuid(), nullable=False),
        sa.Column("version", sa.INTEGER(), nullable=False),
        sa.Column("rules", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("checksum", sa.CHAR(length=64), nullable=False),
        sa.Column("is_active", sa.BOOLEAN(), server_default=sa.text("true"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["repository_id"],
            ["repositories.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("repository_id", "version"),
    )
    op.create_table(
        "runs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("code_change_id", sa.Uuid(), nullable=False),
        sa.Column("base_sha", sa.String(length=64), nullable=False),
        sa.Column("head_sha", sa.String(length=64), nullable=False),
        sa.Column(
            "state",
            postgresql.ENUM(
                "queued",
                "running",
                "publishing",
                "succeeded",
                "failed",
                "cancelled",
                "skipped",
                name="run_state",
                create_type=False,
            ),
            server_default="queued",
            nullable=False,
        ),
        sa.Column("trigger", sa.String(length=50), nullable=False),
        sa.Column("idempotency_key", sa.CHAR(length=64), nullable=False),
        sa.Column(
            "engine",
            postgresql.ENUM("fast", "deep", name="engine", create_type=False),
            nullable=False,
        ),
        sa.Column("rule_version_id", sa.Uuid(), nullable=False),
        sa.Column("prompt_version_id", sa.Uuid(), nullable=False),
        sa.Column("attempt", sa.INTEGER(), server_default=sa.text("0"), nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("worker_id", sa.String(length=255), nullable=True),
        sa.Column(
            "cancel_requested", sa.BOOLEAN(), server_default=sa.text("false"), nullable=False
        ),
        sa.Column("review_body", sa.TEXT(), nullable=True),
        sa.Column("error_code", sa.String(length=100), nullable=True),
        sa.Column("error_message", sa.TEXT(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("attempt >= 0", name="ck_runs_attempt_nonnegative"),
        sa.ForeignKeyConstraint(
            ["code_change_id"],
            ["code_changes.id"],
        ),
        sa.ForeignKeyConstraint(
            ["prompt_version_id"],
            ["prompt_versions.id"],
        ),
        sa.ForeignKeyConstraint(
            ["rule_version_id"],
            ["rule_versions.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("idempotency_key"),
    )
    op.create_table(
        "context_payloads",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("schema_version", sa.INTEGER(), nullable=False),
        sa.Column("summary", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("s3_ref", sa.TEXT(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["runs.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("run_id"),
    )
    op.create_table(
        "findings",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("file_path", sa.String(length=1024), nullable=False),
        sa.Column("line_start", sa.INTEGER(), nullable=False),
        sa.Column("line_end", sa.INTEGER(), nullable=True),
        sa.Column("side", sa.String(length=10), server_default=sa.text("'RIGHT'"), nullable=False),
        sa.Column("severity", sa.String(length=30), nullable=False),
        sa.Column("confidence", sa.Numeric(precision=3, scale=2), nullable=False),
        sa.Column("category", sa.String(length=30), nullable=False),
        sa.Column("suggestion", sa.TEXT(), nullable=True),
        sa.Column("title", sa.String(length=500), nullable=False),
        sa.Column("body", sa.TEXT(), nullable=False),
        sa.Column("rule_name", sa.String(length=255), nullable=True),
        sa.Column("published", sa.BOOLEAN(), server_default=sa.text("false"), nullable=False),
        sa.Column("drop_reason", sa.String(length=100), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "confidence >= 0 AND confidence <= 1", name="ck_findings_confidence_range"
        ),
        sa.CheckConstraint(
            "line_end IS NULL OR line_end >= line_start", name="ck_findings_line_range"
        ),
        sa.CheckConstraint("line_start > 0", name="ck_findings_line_start_positive"),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["runs.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "run_actions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("index", sa.INTEGER(), nullable=False),
        sa.Column("tool", sa.String(length=100), nullable=False),
        sa.Column("request", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("response_ref", sa.TEXT(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("duration_ms", sa.INTEGER(), nullable=False),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["runs.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("run_id", "index"),
    )
    op.create_table(
        "usage_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("provider", sa.String(length=50), nullable=False),
        sa.Column("model", sa.String(length=100), nullable=False),
        sa.Column("operation", sa.String(length=50), nullable=False),
        sa.Column("tokens_in", sa.INTEGER(), nullable=False),
        sa.Column("tokens_out", sa.INTEGER(), nullable=False),
        sa.Column("cache_read_tokens", sa.INTEGER(), server_default=sa.text("0"), nullable=False),
        sa.Column("cost_usd", sa.Numeric(precision=12, scale=6), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["runs.id"],
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "comments",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("finding_id", sa.Uuid(), nullable=False),
        sa.Column("github_review_id", sa.BIGINT(), nullable=False),
        sa.Column("github_comment_id", sa.BIGINT(), nullable=False),
        sa.Column("findings_hash", sa.CHAR(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["finding_id"],
            ["findings.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("finding_id"),
        sa.UniqueConstraint("github_comment_id"),
    )
    op.create_table(
        "credit_ledger",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column(
            "kind",
            postgresql.ENUM(
                "top_up", "usage_debit", "adjustment", name="ledger_kind", create_type=False
            ),
            nullable=False,
        ),
        sa.Column("amount_usd", sa.Numeric(precision=12, scale=6), nullable=False),
        sa.Column("usage_event_id", sa.Uuid(), nullable=True),
        sa.Column("payment_id", sa.Uuid(), nullable=True),
        sa.Column("reason", sa.TEXT(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "kind != 'top_up' OR (amount_usd > 0 AND payment_id IS NOT NULL)",
            name="ck_credit_ledger_top_up",
        ),
        sa.CheckConstraint(
            "kind != 'usage_debit' OR (amount_usd < 0 AND usage_event_id IS NOT NULL)",
            name="ck_credit_ledger_usage_debit",
        ),
        sa.ForeignKeyConstraint(
            ["payment_id"],
            ["payments.id"],
        ),
        sa.ForeignKeyConstraint(
            ["usage_event_id"],
            ["usage_events.id"],
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_prompt_versions_active_key",
        "prompt_versions",
        ["key"],
        unique=True,
        postgresql_where=sa.text("is_active"),
    )
    op.create_index(
        "uq_payments_provider_checkout",
        "payments",
        ["provider", "provider_checkout_id"],
        unique=True,
        postgresql_where=sa.text("provider_checkout_id IS NOT NULL"),
    )
    op.create_index(
        "uq_payments_provider_payment",
        "payments",
        ["provider", "provider_payment_id"],
        unique=True,
        postgresql_where=sa.text("provider_payment_id IS NOT NULL"),
    )
    op.create_index(
        "ix_provider_installations_workspace_id",
        "provider_installations",
        ["workspace_id"],
        unique=False,
    )
    op.create_index(
        "ix_code_changes_repository_state_updated",
        "code_changes",
        ["repository_id", "state", "updated_at"],
        unique=False,
    )
    op.create_index(
        "uq_rule_versions_active_repository",
        "rule_versions",
        ["repository_id"],
        unique=True,
        postgresql_where=sa.text("is_active"),
    )
    op.create_index(
        "ix_runs_queued_available_at",
        "runs",
        ["available_at"],
        unique=False,
        postgresql_where=sa.text("state = 'queued'"),
    )
    op.create_index(
        "uq_runs_one_active_per_code_change",
        "runs",
        ["code_change_id"],
        unique=True,
        postgresql_where=sa.text("state IN ('queued', 'running', 'publishing')"),
    )
    op.create_index("ix_findings_run_id", "findings", ["run_id"], unique=False)
    op.create_index(
        "ix_usage_events_workspace_created",
        "usage_events",
        ["workspace_id", "created_at"],
        unique=False,
    )
    op.create_index(
        "uq_credit_ledger_payment",
        "credit_ledger",
        ["payment_id"],
        unique=True,
        postgresql_where=sa.text("payment_id IS NOT NULL"),
    )
    op.create_index(
        "uq_credit_ledger_usage_event",
        "credit_ledger",
        ["usage_event_id"],
        unique=True,
        postgresql_where=sa.text("usage_event_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_credit_ledger_usage_event",
        table_name="credit_ledger",
        postgresql_where=sa.text("usage_event_id IS NOT NULL"),
    )
    op.drop_index(
        "uq_credit_ledger_payment",
        table_name="credit_ledger",
        postgresql_where=sa.text("payment_id IS NOT NULL"),
    )
    op.drop_index("ix_usage_events_workspace_created", table_name="usage_events")
    op.drop_index("ix_findings_run_id", table_name="findings")
    op.drop_index(
        "uq_runs_one_active_per_code_change",
        table_name="runs",
        postgresql_where=sa.text("state IN ('queued', 'running', 'publishing')"),
    )
    op.drop_index(
        "ix_runs_queued_available_at",
        table_name="runs",
        postgresql_where=sa.text("state = 'queued'"),
    )
    op.drop_index(
        "uq_rule_versions_active_repository",
        table_name="rule_versions",
        postgresql_where=sa.text("is_active"),
    )
    op.drop_index("ix_code_changes_repository_state_updated", table_name="code_changes")
    op.drop_index("ix_provider_installations_workspace_id", table_name="provider_installations")
    op.drop_index(
        "uq_payments_provider_payment",
        table_name="payments",
        postgresql_where=sa.text("provider_payment_id IS NOT NULL"),
    )
    op.drop_index(
        "uq_payments_provider_checkout",
        table_name="payments",
        postgresql_where=sa.text("provider_checkout_id IS NOT NULL"),
    )
    op.drop_index(
        "uq_prompt_versions_active_key",
        table_name="prompt_versions",
        postgresql_where=sa.text("is_active"),
    )
    op.drop_table("credit_ledger")
    op.drop_table("comments")
    op.drop_table("usage_events")
    op.drop_table("run_actions")
    op.drop_table("findings")
    op.drop_table("context_payloads")
    op.drop_table("runs")
    op.drop_table("rule_versions")
    op.drop_table("repo_conventions")
    op.drop_table("code_changes")
    op.drop_table("webhook_events")
    op.drop_table("repositories")
    op.drop_table("provider_installations")
    op.drop_table("payments")
    op.drop_table("workspaces")
    op.drop_table("prompt_versions")
    postgresql.ENUM("top_up", "usage_debit", "adjustment", name="ledger_kind").drop(
        op.get_bind(), checkfirst=False
    )
    postgresql.ENUM(
        "queued",
        "running",
        "publishing",
        "succeeded",
        "failed",
        "cancelled",
        "skipped",
        name="run_state",
    ).drop(op.get_bind(), checkfirst=False)
    postgresql.ENUM("open", "closed", "merged", name="code_change_state").drop(
        op.get_bind(), checkfirst=False
    )
    postgresql.ENUM("comment", "request_changes", name="review_event").drop(
        op.get_bind(), checkfirst=False
    )
    postgresql.ENUM("auto", "always", "never", name="wait_for_ci").drop(
        op.get_bind(), checkfirst=False
    )
    postgresql.ENUM("fast", "deep", name="engine").drop(op.get_bind(), checkfirst=False)
    postgresql.ENUM("pending", "paid", "failed", "expired", name="payment_status").drop(
        op.get_bind(), checkfirst=False
    )
