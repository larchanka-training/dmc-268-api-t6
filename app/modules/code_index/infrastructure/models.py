"""SQLAlchemy 2 persistence models of the graph index (ADR 0001 §4 and §6.2, #141).

The index keeps pointers only: paths, qualified names, symbol kinds, line ranges, blob
and commit shas, and edges. It stores no code text, signatures or embeddings. Every row
carries `repository_id` with a cascading FK; graph rows are versioned by `generation`
and the publication range `valid_from`/`valid_to`, under natural composite keys.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    BIGINT,
    INTEGER,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    Uuid,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.common.infrastructure.db.base import Base
from app.common.infrastructure.db.columns import pg_enum, timestamp_column
from app.modules.code_index.domain.enums import EdgeKind, IndexStatus, PurgeReason
from app.modules.reviews.application.ast_resolution import ImportResolution
from app.modules.reviews.application.ast_symbols import SymbolKind


class CodeIndexState(Base):
    """One row per repository; it is never deleted except by the repository FK cascade."""

    __tablename__ = "code_index_states"
    __table_args__ = (
        CheckConstraint(
            "(status = 'failed') = (error_code IS NOT NULL)",
            name="ck_code_index_states_failed_error",
        ),
        CheckConstraint(
            "(lease_token IS NULL) = (lease_expires_at IS NULL)",
            name="ck_code_index_states_lease_pair",
        ),
        CheckConstraint(
            "purge_reason IS NULL OR status = 'empty'", name="ck_code_index_states_purge_reason"
        ),
        # A publication records all six versions of its profile; any other row records none.
        CheckConstraint(
            "num_nulls(extractor_version, resolver_version, parser_profile_version, "
            "limits_profile_version, indexed_default_branch, exclusions_digest) IN (0, 6)",
            name="ck_code_index_states_profile_complete",
        ),
        CheckConstraint(
            "stored_bytes >= 0 AND pending_bytes >= 0", name="ck_code_index_states_bytes"
        ),
        CheckConstraint(
            "pending_bytes = 0 OR lease_token IS NOT NULL",
            name="ck_code_index_states_pending_leased",
        ),
        # ADR §7: only a limit or an unsafe archive fails an index.
        CheckConstraint(
            "status <> 'failed' OR error_code = 'archive_unsafe' "
            "OR error_code LIKE 'limit_exceeded:%'",
            name="ck_code_index_states_failed_code",
        ),
        # Only a live index waits for a wake-up: a purge and a failure clear the marker.
        CheckConstraint(
            "wake_requested_at IS NULL OR (purge_reason IS NULL AND status <> 'failed')",
            name="ck_code_index_states_wake_live",
        ),
        # Admission counts the live leases of all repositories (ADR §7).
        Index(
            "ix_code_index_states_live_lease",
            "repository_id",
            postgresql_where=text("lease_token IS NOT NULL"),
        ),
        # The leader dispatches the marked rows (#143).
        Index(
            "ix_code_index_states_wake_requested",
            "wake_requested_at",
            postgresql_where=text("wake_requested_at IS NOT NULL"),
        ),
    )

    repository_id: Mapped[UUID] = mapped_column(
        ForeignKey("repositories.id", ondelete="CASCADE"), primary_key=True
    )
    generation: Mapped[int] = mapped_column(BIGINT, nullable=False, server_default=text("0"))
    reserved_publication: Mapped[int] = mapped_column(
        BIGINT, nullable=False, server_default=text("0")
    )
    published_publication: Mapped[int] = mapped_column(
        BIGINT, nullable=False, server_default=text("0")
    )
    floor_publication: Mapped[int] = mapped_column(BIGINT, nullable=False, server_default=text("0"))
    status: Mapped[IndexStatus] = mapped_column(
        pg_enum(IndexStatus, "code_index_status"),
        nullable=False,
        server_default=IndexStatus.EMPTY.value,
    )
    purge_reason: Mapped[PurgeReason | None] = mapped_column(
        pg_enum(PurgeReason, "code_index_purge_reason")
    )
    error_code: Mapped[str | None] = mapped_column(String(64))
    last_error_code: Mapped[str | None] = mapped_column(String(64))
    indexed_commit_sha: Mapped[str | None] = mapped_column(String(64))
    target_commit_sha: Mapped[str | None] = mapped_column(String(64))
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_token: Mapped[UUID | None] = mapped_column(Uuid)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    file_count: Mapped[int] = mapped_column(INTEGER, nullable=False, server_default=text("0"))
    symbol_count: Mapped[int] = mapped_column(INTEGER, nullable=False, server_default=text("0"))
    edge_count: Mapped[int] = mapped_column(INTEGER, nullable=False, server_default=text("0"))
    extractor_version: Mapped[str | None] = mapped_column(String(64))
    resolver_version: Mapped[str | None] = mapped_column(String(64))
    parser_profile_version: Mapped[str | None] = mapped_column(String(64))
    limits_profile_version: Mapped[str | None] = mapped_column(String(64))
    downloaded_bytes: Mapped[int] = mapped_column(BIGINT, nullable=False, server_default=text("0"))
    build_duration_ms: Mapped[int] = mapped_column(
        INTEGER, nullable=False, server_default=text("0")
    )
    github_request_count: Mapped[int] = mapped_column(
        INTEGER, nullable=False, server_default=text("0")
    )
    created_at: Mapped[datetime] = timestamp_column()
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
    indexed_default_branch: Mapped[str | None] = mapped_column(String(255))
    # `IgnorePolicy.digest` of the `.review/rules.md` the publication was built with.
    exclusions_digest: Mapped[str | None] = mapped_column(String(64))
    # Estimated bytes (`domain/storage.py`) of the published rows and of the leased
    # attempt's rows: the storage quotas of ADR §7.
    stored_bytes: Mapped[int] = mapped_column(BIGINT, nullable=False, server_default=text("0"))
    pending_bytes: Mapped[int] = mapped_column(BIGINT, nullable=False, server_default=text("0"))
    # The outbox of index wake-ups (#143): a trigger sets it in its own transaction, and
    # the worker leader wakes the job after the commit and clears it with a compare-and-set.
    wake_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class CodeIndexFile(Base):
    __tablename__ = "code_index_files"
    __table_args__ = (
        # The leader's garbage probe of closed rows (#143).
        Index(
            "ix_code_index_files_closed",
            "repository_id",
            postgresql_where=text("valid_to IS NOT NULL"),
        ),
    )

    repository_id: Mapped[UUID] = mapped_column(
        ForeignKey("repositories.id", ondelete="CASCADE"), primary_key=True
    )
    generation: Mapped[int] = mapped_column(BIGINT, primary_key=True)
    valid_from: Mapped[int] = mapped_column(BIGINT, primary_key=True)
    valid_to: Mapped[int | None] = mapped_column(BIGINT)
    path: Mapped[str] = mapped_column(String(1024), primary_key=True)
    blob_sha: Mapped[str] = mapped_column(String(64), nullable=False)


class CodeIndexSymbol(Base):
    """A declaration; `defines` (ADR §3) is this row itself, not an edge row."""

    __tablename__ = "code_index_symbols"
    __table_args__ = (
        CheckConstraint(
            "start_line >= 1 AND end_line >= start_line", name="ck_code_index_symbols_lines"
        ),
    )

    repository_id: Mapped[UUID] = mapped_column(
        ForeignKey("repositories.id", ondelete="CASCADE"), primary_key=True
    )
    generation: Mapped[int] = mapped_column(BIGINT, primary_key=True)
    valid_from: Mapped[int] = mapped_column(BIGINT, primary_key=True)
    valid_to: Mapped[int | None] = mapped_column(BIGINT)
    path: Mapped[str] = mapped_column(String(1024), primary_key=True)
    qualified_name: Mapped[str] = mapped_column(String(512), primary_key=True)
    kind: Mapped[SymbolKind] = mapped_column(
        pg_enum(SymbolKind, "code_index_symbol_kind"), primary_key=True
    )
    # 0-based ordinal among duplicates of (path, qualified_name, kind); a unique symbol is 0.
    disambiguator: Mapped[int] = mapped_column(INTEGER, primary_key=True)
    start_line: Mapped[int] = mapped_column(INTEGER, nullable=False)
    end_line: Mapped[int] = mapped_column(INTEGER, nullable=False)


class CodeIndexEdge(Base):
    """An edge to (target_path, target_name); `imports` edges carry an empty name."""

    __tablename__ = "code_index_edges"
    __table_args__ = (
        CheckConstraint(
            "(kind = 'imports') = (target_name = '')", name="ck_code_index_edges_target_name"
        ),
        Index(
            "ix_code_index_edges_target",
            "repository_id",
            "generation",
            "target_path",
            "target_name",
        ),
    )

    repository_id: Mapped[UUID] = mapped_column(
        ForeignKey("repositories.id", ondelete="CASCADE"), primary_key=True
    )
    generation: Mapped[int] = mapped_column(BIGINT, primary_key=True)
    valid_from: Mapped[int] = mapped_column(BIGINT, primary_key=True)
    valid_to: Mapped[int | None] = mapped_column(BIGINT)
    path: Mapped[str] = mapped_column(String(1024), primary_key=True)
    kind: Mapped[EdgeKind] = mapped_column(
        pg_enum(EdgeKind, "code_index_edge_kind"), primary_key=True
    )
    target_path: Mapped[str] = mapped_column(String(1024), primary_key=True)
    target_name: Mapped[str] = mapped_column(String(512), primary_key=True)


class CodeIndexImport(Base):
    """One import record of an indexed file with its outcome, resolved or not."""

    __tablename__ = "code_index_imports"
    __table_args__ = (
        Index("ix_code_index_imports_target", "repository_id", "generation", "target_path"),
    )

    repository_id: Mapped[UUID] = mapped_column(
        ForeignKey("repositories.id", ondelete="CASCADE"), primary_key=True
    )
    generation: Mapped[int] = mapped_column(BIGINT, primary_key=True)
    valid_from: Mapped[int] = mapped_column(BIGINT, primary_key=True)
    valid_to: Mapped[int | None] = mapped_column(BIGINT)
    path: Mapped[str] = mapped_column(String(1024), primary_key=True)
    specifier: Mapped[str] = mapped_column(String(1024), primary_key=True)
    resolution: Mapped[ImportResolution] = mapped_column(
        pg_enum(ImportResolution, "code_index_import_resolution"), primary_key=True
    )
    target_path: Mapped[str | None] = mapped_column(String(1024))
