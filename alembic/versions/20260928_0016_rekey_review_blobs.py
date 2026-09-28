"""Retain per-file VCS metadata and share immutable blobs by repository and SHA.

Revision ID: 20260928_0016
Revises: 20260928_0015
Create Date: 2026-09-28
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "20260928_0016"
down_revision = "20260928_0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("runs", sa.Column("diff_snapshotted_at", sa.DateTime(timezone=True)))
    op.add_column("code_change_diffs", sa.Column("run_id", sa.Uuid()))
    op.drop_constraint(
        "code_change_diffs_code_change_id_head_sha_filename_key",
        "code_change_diffs",
        type_="unique",
    )
    op.execute(
        "UPDATE code_change_diffs AS diff SET run_id = ("
        "SELECT run.id FROM runs AS run "
        "WHERE run.code_change_id = diff.code_change_id AND run.head_sha = diff.head_sha "
        "ORDER BY run.created_at, run.id LIMIT 1)"
    )
    # An old snapshot without any matching Run had no valid run owner.
    op.execute("DELETE FROM code_change_diffs WHERE run_id IS NULL")
    op.execute(
        "INSERT INTO code_change_diffs "
        "(id, code_change_id, head_sha, filename, patch, created_at, run_id) "
        "SELECT md5(diff.id::text || run.id::text)::uuid, diff.code_change_id, "
        "diff.head_sha, diff.filename, diff.patch, diff.created_at, run.id "
        "FROM code_change_diffs AS diff JOIN runs AS run "
        "ON run.code_change_id = diff.code_change_id AND run.head_sha = diff.head_sha "
        "WHERE run.id <> diff.run_id"
    )
    op.execute(
        "UPDATE runs AS run SET diff_snapshotted_at = now() "
        "WHERE EXISTS (SELECT 1 FROM code_change_diffs AS diff WHERE diff.run_id = run.id)"
    )
    op.alter_column("code_change_diffs", "run_id", nullable=False)
    op.create_foreign_key(
        "fk_code_change_diffs_run_id_runs",
        "code_change_diffs",
        "runs",
        ["run_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_unique_constraint(
        "uq_code_change_diffs_run_filename", "code_change_diffs", ["run_id", "filename"]
    )
    op.add_column("code_change_diffs", sa.Column("review_patch", sa.TEXT()))
    op.add_column("code_change_diffs", sa.Column("blob_sha", sa.String(length=64)))
    op.add_column(
        "code_change_diffs",
        sa.Column("status", sa.String(length=16), server_default="modified", nullable=False),
    )
    op.add_column("code_change_diffs", sa.Column("previous_filename", sa.String(length=1024)))
    for name in ("additions", "deletions", "changes"):
        op.add_column(
            "code_change_diffs",
            sa.Column(name, sa.Integer(), server_default="0", nullable=False),
        )
    op.add_column("code_change_diffs", sa.Column("omission_reason", sa.String(length=32)))
    # Old snapshots could contain generated files and a synthetic binary patch.
    # Preserve their UI patch, but keep them out of model input on Run retry.
    op.execute(
        "UPDATE code_change_diffs SET omission_reason = 'generated' "
        "WHERE lower(filename) ~ '(^|/)(package-lock[.]json|pnpm-lock[.]yaml|"
        "yarn[.]lock|bun[.]lockb?|cargo[.]lock|gemfile[.]lock|poetry[.]lock|"
        "uv[.]lock|pipfile[.]lock|composer[.]lock|go[.]sum)$' "
        "OR lower(filename) ~ '[.](lock|lockb|min[.]js|pb[.]go|snap)$' "
        "OR lower(filename) ~ '(^|/)(dist|__snapshots__|locale|locales|i18n|"
        "l10n|translations)/' "
        "OR (lower(filename) ~ '(^|/)migrations/' "
        "AND lower(filename) ~ '(^|/)[^/]*snapshot[^/]*$')"
    )
    op.execute(
        "UPDATE code_change_diffs SET omission_reason = 'binary' "
        "WHERE omission_reason IS NULL AND patch = "
        "'diff --git a/' || filename || ' b/' || filename || E'\\n' || "
        "'Binary files a/' || filename || ' and b/' || filename || ' differ'"
    )
    op.add_column(
        "code_change_diffs",
        sa.Column("summary_only", sa.Boolean(), server_default=sa.false(), nullable=False),
    )
    op.create_check_constraint(
        "ck_code_change_diffs_status",
        "code_change_diffs",
        "status IN ('added', 'modified', 'removed', 'renamed')",
    )
    op.create_check_constraint(
        "ck_code_change_diffs_omission_reason",
        "code_change_diffs",
        "omission_reason IS NULL OR omission_reason IN "
        "('binary', 'too_large', 'generated', 'missing_patch')",
    )

    # Cached blobs are disposable seven-day data. Old path/ref entries cannot be
    # safely mapped to immutable blob SHAs, so discard them during the re-key.
    op.drop_table("cached_file_blobs")
    op.create_table(
        "cached_file_blobs",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "repository_id",
            sa.Uuid(),
            sa.ForeignKey("repositories.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("blob_sha", sa.String(length=64), nullable=False),
        sa.Column("content", sa.TEXT(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.UniqueConstraint("repository_id", "blob_sha"),
    )


def downgrade() -> None:
    op.drop_table("cached_file_blobs")
    op.create_table(
        "cached_file_blobs",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "code_change_id",
            sa.Uuid(),
            sa.ForeignKey("code_changes.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("head_sha", sa.String(length=64), nullable=False),
        sa.Column("path", sa.String(length=1024), nullable=False),
        sa.Column("content", sa.TEXT(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.UniqueConstraint("code_change_id", "head_sha", "path"),
    )
    op.drop_constraint("ck_code_change_diffs_omission_reason", "code_change_diffs")
    op.drop_constraint("ck_code_change_diffs_status", "code_change_diffs")
    for name in (
        "summary_only",
        "omission_reason",
        "changes",
        "deletions",
        "additions",
        "previous_filename",
        "status",
        "blob_sha",
        "review_patch",
    ):
        op.drop_column("code_change_diffs", name)
    # The old schema can keep only one row per PR/head/path. Retain one
    # deterministic Run copy; later Run-specific snapshots cannot be represented.
    op.execute(
        "DELETE FROM code_change_diffs AS diff USING code_change_diffs AS earlier "
        "WHERE diff.code_change_id = earlier.code_change_id "
        "AND diff.head_sha = earlier.head_sha AND diff.filename = earlier.filename "
        "AND diff.run_id > earlier.run_id"
    )
    op.drop_constraint("uq_code_change_diffs_run_filename", "code_change_diffs", type_="unique")
    op.drop_constraint("fk_code_change_diffs_run_id_runs", "code_change_diffs", type_="foreignkey")
    op.drop_column("code_change_diffs", "run_id")
    op.create_unique_constraint(
        "code_change_diffs_code_change_id_head_sha_filename_key",
        "code_change_diffs",
        ["code_change_id", "head_sha", "filename"],
    )
    op.drop_column("runs", "diff_snapshotted_at")
