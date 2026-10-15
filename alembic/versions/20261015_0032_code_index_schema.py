"""Store the repository-scoped graph index: state, files, symbols, edges, imports (#141).

Revision ID: 20261015_0032
Revises: 20261014_0031
"""

from __future__ import annotations

from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "20261015_0032"
down_revision = "20261014_0031"
branch_labels = None
depends_on = None

_ENUMS = {
    "code_index_status": ("empty", "building", "ready", "failed"),
    "code_index_purge_reason": ("ttl", "removed", "disabled"),
    "code_index_symbol_kind": ("function", "method", "class", "variable", "type"),
    "code_index_edge_kind": ("imports", "uses_imported"),
    "code_index_import_resolution": (
        "resolved",
        "external",
        "ambiguous",
        "dynamic",
        "excluded",
        "unsupported",
        "missing",
    ),
}


def _enum(name: str) -> postgresql.ENUM:
    return postgresql.ENUM(*_ENUMS[name], name=name, create_type=False)


def _graph_key() -> list[sa.Column[Any]]:
    return [
        sa.Column(
            "repository_id",
            sa.Uuid(),
            sa.ForeignKey("repositories.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("generation", sa.BIGINT(), primary_key=True),
        sa.Column("valid_from", sa.BIGINT(), primary_key=True),
        sa.Column("valid_to", sa.BIGINT()),
        sa.Column("path", sa.String(1024), primary_key=True),
    ]


def upgrade() -> None:
    for name, values in _ENUMS.items():
        postgresql.ENUM(*values, name=name).create(op.get_bind(), checkfirst=False)
    op.create_table(
        "code_index_states",
        sa.Column(
            "repository_id",
            sa.Uuid(),
            sa.ForeignKey("repositories.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("generation", sa.BIGINT(), nullable=False, server_default=sa.text("0")),
        sa.Column("reserved_publication", sa.BIGINT(), nullable=False, server_default=sa.text("0")),
        sa.Column(
            "published_publication", sa.BIGINT(), nullable=False, server_default=sa.text("0")
        ),
        sa.Column("floor_publication", sa.BIGINT(), nullable=False, server_default=sa.text("0")),
        sa.Column("status", _enum("code_index_status"), nullable=False, server_default="empty"),
        sa.Column("purge_reason", _enum("code_index_purge_reason")),
        sa.Column("error_code", sa.String(64)),
        sa.Column("last_error_code", sa.String(64)),
        sa.Column("indexed_commit_sha", sa.String(64)),
        sa.Column("target_commit_sha", sa.String(64)),
        sa.Column("published_at", sa.DateTime(timezone=True)),
        sa.Column("last_used_at", sa.DateTime(timezone=True)),
        sa.Column("lease_token", sa.Uuid()),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True)),
        sa.Column("file_count", sa.INTEGER(), nullable=False, server_default=sa.text("0")),
        sa.Column("symbol_count", sa.INTEGER(), nullable=False, server_default=sa.text("0")),
        sa.Column("edge_count", sa.INTEGER(), nullable=False, server_default=sa.text("0")),
        sa.Column("extractor_version", sa.String(64)),
        sa.Column("resolver_version", sa.String(64)),
        sa.Column("parser_profile_version", sa.String(64)),
        sa.Column("limits_profile_version", sa.String(64)),
        sa.Column("downloaded_bytes", sa.BIGINT(), nullable=False, server_default=sa.text("0")),
        sa.Column("build_duration_ms", sa.INTEGER(), nullable=False, server_default=sa.text("0")),
        sa.Column(
            "github_request_count", sa.INTEGER(), nullable=False, server_default=sa.text("0")
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint(
            "(status = 'failed') = (error_code IS NOT NULL)",
            name="ck_code_index_states_failed_error",
        ),
        sa.CheckConstraint(
            "(lease_token IS NULL) = (lease_expires_at IS NULL)",
            name="ck_code_index_states_lease_pair",
        ),
        sa.CheckConstraint(
            "purge_reason IS NULL OR status = 'empty'", name="ck_code_index_states_purge_reason"
        ),
    )
    op.create_table(
        "code_index_files",
        *_graph_key(),
        sa.Column("blob_sha", sa.String(64), nullable=False),
    )
    op.create_table(
        "code_index_symbols",
        *_graph_key(),
        sa.Column("qualified_name", sa.String(512), primary_key=True),
        sa.Column("kind", _enum("code_index_symbol_kind"), primary_key=True),
        sa.Column("disambiguator", sa.INTEGER(), primary_key=True),
        sa.Column("start_line", sa.INTEGER(), nullable=False),
        sa.Column("end_line", sa.INTEGER(), nullable=False),
        sa.CheckConstraint(
            "start_line >= 1 AND end_line >= start_line", name="ck_code_index_symbols_lines"
        ),
    )
    op.create_table(
        "code_index_edges",
        *_graph_key(),
        sa.Column("kind", _enum("code_index_edge_kind"), primary_key=True),
        sa.Column("target_path", sa.String(1024), primary_key=True),
        sa.Column("target_name", sa.String(512), primary_key=True),
        sa.CheckConstraint(
            "(kind = 'imports') = (target_name = '')", name="ck_code_index_edges_target_name"
        ),
    )
    op.create_index(
        "ix_code_index_edges_target",
        "code_index_edges",
        ["repository_id", "generation", "target_path", "target_name"],
    )
    op.create_table(
        "code_index_imports",
        *_graph_key(),
        sa.Column("specifier", sa.String(1024), primary_key=True),
        sa.Column("resolution", _enum("code_index_import_resolution"), primary_key=True),
        sa.Column("target_path", sa.String(1024)),
    )
    op.create_index(
        "ix_code_index_imports_target",
        "code_index_imports",
        ["repository_id", "generation", "target_path"],
    )


def downgrade() -> None:
    op.drop_table("code_index_imports")
    op.drop_table("code_index_edges")
    op.drop_table("code_index_symbols")
    op.drop_table("code_index_files")
    op.drop_table("code_index_states")
    for name, values in reversed(_ENUMS.items()):
        postgresql.ENUM(*values, name=name).drop(op.get_bind(), checkfirst=False)
