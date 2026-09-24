"""SQLAlchemy 2 persistence models for the initial PostgreSQL schema.

These are deliberately separate from HTTP, messaging and domain DTOs.  Their table
and column names are the stable persistence contract used by Alembic.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    BIGINT,
    BOOLEAN,
    CHAR,
    INTEGER,
    TEXT,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Numeric,
    String,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.common.infrastructure.db.base import Base
from app.common.infrastructure.db.columns import pg_enum, timestamp_column
from app.common.infrastructure.db.enums import (
    CodeChangeState,
    Engine,
    FindingCategory,
    FindingSeverity,
    FindingSide,
    RunState,
)


class PromptVersion(Base):
    __tablename__ = "prompt_versions"
    __table_args__ = (
        Index(
            "uq_prompt_versions_active_key",
            "key",
            unique=True,
            postgresql_where=text("is_active"),
        ),
        UniqueConstraint("key", "version"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    key: Mapped[str] = mapped_column(String(100), nullable=False)
    version: Mapped[int] = mapped_column(INTEGER, nullable=False)
    content: Mapped[str] = mapped_column(TEXT, nullable=False)
    checksum: Mapped[str] = mapped_column(CHAR(64), nullable=False, unique=True)
    is_active: Mapped[bool] = mapped_column(BOOLEAN, nullable=False, server_default=text("false"))
    created_at: Mapped[datetime] = timestamp_column()


class CodeChange(Base):
    __tablename__ = "code_changes"
    __table_args__ = (
        UniqueConstraint("repository_id", "external_id"),
        UniqueConstraint("repository_id", "external_number"),
        Index("ix_code_changes_repository_state_updated", "repository_id", "state", "updated_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    repository_id: Mapped[UUID] = mapped_column(ForeignKey("repositories.id"), nullable=False)
    external_id: Mapped[int] = mapped_column(BIGINT, nullable=False)
    external_number: Mapped[int] = mapped_column(INTEGER, nullable=False)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    description: Mapped[str | None] = mapped_column(TEXT)
    source_branch: Mapped[str] = mapped_column(String(255), nullable=False)
    target_branch: Mapped[str] = mapped_column(String(255), nullable=False)
    base_sha: Mapped[str] = mapped_column(String(64), nullable=False)
    head_sha: Mapped[str] = mapped_column(String(64), nullable=False)
    state: Mapped[CodeChangeState] = mapped_column(pg_enum(CodeChangeState, "code_change_state"))
    reviewer_requested: Mapped[bool] = mapped_column(
        BOOLEAN, nullable=False, server_default=text("false")
    )
    ci_status: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'")
    )
    web_url: Mapped[str] = mapped_column(TEXT, nullable=False)
    created_at: Mapped[datetime] = timestamp_column()
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class CodeChangeDiff(Base):
    __tablename__ = "code_change_diffs"
    __table_args__ = (UniqueConstraint("code_change_id", "head_sha", "filename"),)

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    code_change_id: Mapped[UUID] = mapped_column(
        ForeignKey("code_changes.id", ondelete="CASCADE"), nullable=False
    )
    head_sha: Mapped[str] = mapped_column(String(64), nullable=False)
    filename: Mapped[str] = mapped_column(String(1024), nullable=False)
    patch: Mapped[str | None] = mapped_column(TEXT)
    created_at: Mapped[datetime] = timestamp_column()


class Run(Base):
    __tablename__ = "runs"
    __table_args__ = (
        UniqueConstraint("idempotency_key"),
        CheckConstraint("attempt >= 0", name="ck_runs_attempt_nonnegative"),
        Index(
            "uq_runs_one_active_per_code_change",
            "code_change_id",
            unique=True,
            postgresql_where=text("state IN ('queued', 'running', 'publishing')"),
        ),
        Index(
            "ix_runs_queued_available_at", "available_at", postgresql_where=text("state = 'queued'")
        ),
        Index("ix_runs_created_id", "created_at", "id"),
        Index("ix_runs_state_created_id", "state", "created_at", "id"),
        Index("ix_runs_code_change_created_id", "code_change_id", "created_at", "id"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    code_change_id: Mapped[UUID] = mapped_column(ForeignKey("code_changes.id"), nullable=False)
    base_sha: Mapped[str] = mapped_column(String(64), nullable=False)
    head_sha: Mapped[str] = mapped_column(String(64), nullable=False)
    state: Mapped[RunState] = mapped_column(
        pg_enum(RunState, "run_state"), nullable=False, server_default=RunState.QUEUED.value
    )
    trigger: Mapped[str] = mapped_column(String(50), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    engine: Mapped[Engine] = mapped_column(pg_enum(Engine, "engine"), nullable=False)
    rule_version_id: Mapped[UUID] = mapped_column(ForeignKey("rule_versions.id"), nullable=False)
    prompt_version_id: Mapped[UUID] = mapped_column(
        ForeignKey("prompt_versions.id"), nullable=False
    )
    attempt: Mapped[int] = mapped_column(INTEGER, nullable=False, server_default=text("0"))
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    worker_id: Mapped[str | None] = mapped_column(String(255))
    cancel_requested: Mapped[bool] = mapped_column(
        BOOLEAN, nullable=False, server_default=text("false")
    )
    review_body: Mapped[str | None] = mapped_column(TEXT)
    error_code: Mapped[str | None] = mapped_column(String(100))
    error_message: Mapped[str | None] = mapped_column(TEXT)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = timestamp_column()
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class ContextPayload(Base):
    __tablename__ = "context_payloads"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    run_id: Mapped[UUID] = mapped_column(ForeignKey("runs.id"), nullable=False, unique=True)
    schema_version: Mapped[int] = mapped_column(INTEGER, nullable=False)
    summary: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    s3_ref: Mapped[str] = mapped_column(TEXT, nullable=False)
    created_at: Mapped[datetime] = timestamp_column()


class Finding(Base):
    __tablename__ = "findings"
    __table_args__ = (
        CheckConstraint("line_start > 0", name="ck_findings_line_start_positive"),
        CheckConstraint(
            "line_end IS NULL OR line_end >= line_start", name="ck_findings_line_range"
        ),
        CheckConstraint("confidence >= 0 AND confidence <= 1", name="ck_findings_confidence_range"),
        Index("ix_findings_run_id", "run_id"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    run_id: Mapped[UUID] = mapped_column(ForeignKey("runs.id"), nullable=False)
    file_path: Mapped[str] = mapped_column(String(1024), nullable=False)
    line_start: Mapped[int] = mapped_column(INTEGER, nullable=False)
    line_end: Mapped[int | None] = mapped_column(INTEGER)
    side: Mapped[FindingSide] = mapped_column(
        pg_enum(FindingSide, "finding_side"), nullable=False, server_default=text("'RIGHT'")
    )
    severity: Mapped[FindingSeverity] = mapped_column(
        pg_enum(FindingSeverity, "finding_severity"), nullable=False
    )
    confidence: Mapped[Decimal] = mapped_column(Numeric(3, 2), nullable=False)
    category: Mapped[FindingCategory] = mapped_column(
        pg_enum(FindingCategory, "finding_category"), nullable=False
    )
    suggestion: Mapped[str | None] = mapped_column(TEXT)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    body: Mapped[str] = mapped_column(TEXT, nullable=False)
    rule_name: Mapped[str | None] = mapped_column(String(255))
    published: Mapped[bool] = mapped_column(BOOLEAN, nullable=False, server_default=text("false"))
    drop_reason: Mapped[str | None] = mapped_column(String(100))
    created_at: Mapped[datetime] = timestamp_column()


class Comment(Base):
    __tablename__ = "comments"
    __table_args__ = (UniqueConstraint("github_comment_id"),)

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    finding_id: Mapped[UUID] = mapped_column(ForeignKey("findings.id"), nullable=False, unique=True)
    github_review_id: Mapped[int] = mapped_column(BIGINT, nullable=False)
    github_comment_id: Mapped[int] = mapped_column(BIGINT, nullable=False)
    findings_hash: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    created_at: Mapped[datetime] = timestamp_column()


class RunAction(Base):
    __tablename__ = "run_actions"
    __table_args__ = (
        UniqueConstraint("run_id", "index"),
        CheckConstraint(
            "response IS NULL OR response_ref IS NULL",
            name="ck_run_actions_response_location",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    run_id: Mapped[UUID] = mapped_column(ForeignKey("runs.id"), nullable=False)
    index: Mapped[int] = mapped_column(INTEGER, nullable=False)
    tool: Mapped[str] = mapped_column(String(100), nullable=False)
    request: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    response: Mapped[Any | None] = mapped_column(JSONB(none_as_null=True))
    response_ref: Mapped[str | None] = mapped_column(TEXT)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    duration_ms: Mapped[int] = mapped_column(INTEGER, nullable=False)
