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
    SMALLINT,
    TEXT,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    LargeBinary,
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
    SeverityThreshold,
)
from app.modules.reviews.application.run_trigger import RunTrigger


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
    author_login: Mapped[str | None] = mapped_column(String(255), nullable=True)
    source_branch: Mapped[str] = mapped_column(String(255), nullable=False)
    target_branch: Mapped[str] = mapped_column(String(255), nullable=False)
    base_sha: Mapped[str] = mapped_column(String(64), nullable=False)
    head_sha: Mapped[str] = mapped_column(String(64), nullable=False)
    state: Mapped[CodeChangeState] = mapped_column(pg_enum(CodeChangeState, "code_change_state"))
    ai_review_labeled: Mapped[bool] = mapped_column(
        BOOLEAN, nullable=False, server_default=text("false")
    )
    ai_review_labeled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    label_intent_updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    reviewer_requested: Mapped[bool] = mapped_column(
        BOOLEAN, nullable=False, server_default=text("false")
    )
    reviewer_requested_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    reviewer_intent_updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    reviewer_timeline_event_id: Mapped[int | None] = mapped_column(BIGINT, nullable=True)
    reviewer_timeline_position: Mapped[int | None] = mapped_column(BIGINT, nullable=True)
    reviewer_barrier_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    reviewer_barrier_position: Mapped[int | None] = mapped_column(BIGINT, nullable=True)
    head_first_seen_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    provider_updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
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
    __table_args__ = (
        UniqueConstraint("run_id", "filename", name="uq_code_change_diffs_run_filename"),
        CheckConstraint(
            "status IN ('added', 'modified', 'removed', 'renamed', "
            "'copied', 'changed', 'unchanged')",
            name="ck_code_change_diffs_status",
        ),
        CheckConstraint(
            "omission_reason IS NULL OR omission_reason IN "
            "('binary', 'too_large', 'generated', 'missing_patch', 'empty_file')",
            name="ck_code_change_diffs_omission_reason",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    run_id: Mapped[UUID] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), nullable=False)
    code_change_id: Mapped[UUID] = mapped_column(
        ForeignKey("code_changes.id", ondelete="CASCADE"), nullable=False
    )
    head_sha: Mapped[str] = mapped_column(String(64), nullable=False)
    filename: Mapped[str] = mapped_column(String(1024), nullable=False)
    patch: Mapped[str | None] = mapped_column(TEXT)
    review_patch: Mapped[str | None] = mapped_column(TEXT)
    blob_sha: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default=text("'modified'")
    )
    previous_filename: Mapped[str | None] = mapped_column(String(1024))
    additions: Mapped[int] = mapped_column(INTEGER, nullable=False, server_default=text("0"))
    deletions: Mapped[int] = mapped_column(INTEGER, nullable=False, server_default=text("0"))
    changes: Mapped[int] = mapped_column(INTEGER, nullable=False, server_default=text("0"))
    omission_reason: Mapped[str | None] = mapped_column(String(32))
    summary_only: Mapped[bool] = mapped_column(
        BOOLEAN, nullable=False, server_default=text("false")
    )
    created_at: Mapped[datetime] = timestamp_column()


class CachedFileBlob(Base):
    """A seven-day immutable file blob retained for run inspection."""

    __tablename__ = "cached_file_blobs"
    __table_args__ = (UniqueConstraint("repository_id", "blob_sha"),)

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    repository_id: Mapped[UUID] = mapped_column(
        ForeignKey("repositories.id", ondelete="CASCADE"), nullable=False
    )
    blob_sha: Mapped[str] = mapped_column(String(64), nullable=False)
    content: Mapped[str] = mapped_column(TEXT, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = timestamp_column()


class Run(Base):
    __tablename__ = "runs"
    __table_args__ = (
        UniqueConstraint("idempotency_key"),
        CheckConstraint(
            "(severity_threshold IS NULL AND max_comments IS NULL) OR "
            "(severity_threshold IS NOT NULL AND max_comments IS NOT NULL AND max_comments > 0)",
            name="ck_runs_publication_settings",
        ),
        CheckConstraint("attempt >= 0", name="ck_runs_attempt_nonnegative"),
        # Same values as RunTrigger; changing them needs a new revision (revisions are frozen).
        CheckConstraint(
            "trigger IN ('webhook', 'manual', 'rerun', 'dry_run')", name="ck_runs_trigger"
        ),
        Index(
            "uq_runs_one_active_per_code_change",
            "code_change_id",
            unique=True,
            postgresql_where=text("state IN ('queued', 'running', 'publishing')"),
        ),
        Index(
            "uq_runs_webhook_code_change_head",
            "code_change_id",
            "head_sha",
            unique=True,
            postgresql_where=text("trigger = 'webhook'"),
        ),
        Index(
            "ix_runs_pending_webhook_publication",
            "created_at",
            "id",
            postgresql_where=text(
                "trigger = 'webhook' AND state = 'queued' AND message_published_at IS NULL"
            ),
        ),
        Index(
            "ix_runs_pending_cancellation_signal",
            "cancellation_signal_requested_at",
            "id",
            postgresql_where=text(
                "cancellation_signal_requested_at IS NOT NULL "
                "AND cancellation_signal_published_at IS NULL"
            ),
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
    # Pinned at creation/backfill; Repository ownership may change after reinstall.
    origin_provider_installation_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("provider_installations.id"), nullable=True
    )
    base_sha: Mapped[str] = mapped_column(String(64), nullable=False)
    base_ref: Mapped[str | None] = mapped_column(String(255), nullable=True)
    head_sha: Mapped[str] = mapped_column(String(64), nullable=False)
    state: Mapped[RunState] = mapped_column(
        pg_enum(RunState, "run_state"), nullable=False, server_default=RunState.QUEUED.value
    )
    trigger: Mapped[RunTrigger] = mapped_column(String(50), nullable=False)
    triggered_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    idempotency_key: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    engine: Mapped[Engine] = mapped_column(pg_enum(Engine, "engine"), nullable=False)
    rule_version_id: Mapped[UUID] = mapped_column(ForeignKey("rule_versions.id"), nullable=False)
    prompt_version_id: Mapped[UUID] = mapped_column(
        ForeignKey("prompt_versions.id"), nullable=False
    )
    # The active conventions and verifier prompts, pinned at creation for every retry (RULES
    # §7.2). NULL for a run created before #130 or when no active row of that key existed.
    conventions_prompt_version_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("prompt_versions.id"), nullable=True
    )
    verifier_prompt_version_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("prompt_versions.id"), nullable=True
    )
    severity_threshold: Mapped[SeverityThreshold | None] = mapped_column(
        pg_enum(SeverityThreshold, "severity_threshold"), nullable=True
    )
    max_comments: Mapped[int | None] = mapped_column(SMALLINT, nullable=True)
    attempt: Mapped[int] = mapped_column(INTEGER, nullable=False, server_default=text("0"))
    message_published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancellation_signal_requested_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    cancellation_signal_published_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    diff_snapshotted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
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


class RunPolicyDocument(Base):
    """One policy document of a run, staged once and never overwritten (RULES §7.2)."""

    __tablename__ = "run_policy_documents"
    __table_args__ = (
        UniqueConstraint("run_id", "path", name="uq_run_policy_documents_run_path"),
        CheckConstraint(
            "presence IN ('present', 'missing')", name="ck_run_policy_documents_presence"
        ),
        CheckConstraint(
            "(presence = 'missing') = (text IS NULL) "
            "AND (presence = 'missing') = (raw_bytes IS NULL) "
            "AND (presence = 'missing') = (raw_sha256 IS NULL) "
            "AND (presence = 'missing') = (text_sha256 IS NULL) "
            "AND (presence = 'missing') = (blob_oid IS NULL)",
            name="ck_run_policy_documents_content_matches_presence",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    run_id: Mapped[UUID] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), nullable=False)
    path: Mapped[str] = mapped_column(String(255), nullable=False)
    presence: Mapped[str] = mapped_column(String(16), nullable=False)
    blob_oid: Mapped[str | None] = mapped_column(String(64))
    raw_sha256: Mapped[str | None] = mapped_column(CHAR(64))
    text_sha256: Mapped[str | None] = mapped_column(CHAR(64))
    raw_bytes: Mapped[bytes | None] = mapped_column(LargeBinary)
    text: Mapped[str | None] = mapped_column(TEXT)
    byte_count: Mapped[int | None] = mapped_column(INTEGER)
    line_count: Mapped[int | None] = mapped_column(INTEGER)
    created_at: Mapped[datetime] = timestamp_column()


class RunPolicySnapshot(Base):
    """The immutable policy of a run; its documents are the staged rows of the same run."""

    __tablename__ = "run_policy_snapshots"
    __table_args__ = (
        CheckConstraint(
            "(severity_threshold IS NULL AND max_comments IS NULL) OR "
            "(severity_threshold IS NOT NULL AND max_comments IS NOT NULL AND max_comments > 0)",
            name="ck_run_policy_snapshots_publication_settings",
        ),
    )

    run_id: Mapped[UUID] = mapped_column(
        ForeignKey("runs.id", ondelete="CASCADE"), primary_key=True
    )
    snapshot_id: Mapped[UUID] = mapped_column(Uuid, nullable=False, unique=True)
    repository_id: Mapped[UUID] = mapped_column(
        ForeignKey("repositories.id", ondelete="CASCADE"), nullable=False
    )
    schema_version: Mapped[int] = mapped_column(INTEGER, nullable=False)
    policy_base_sha: Mapped[str] = mapped_column(String(64), nullable=False)
    ignore_patterns: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    ignore_digest: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    parser_profile: Mapped[str] = mapped_column(String(64), nullable=False)
    parser_version: Mapped[str] = mapped_column(String(64), nullable=False)
    matcher_version: Mapped[str] = mapped_column(String(64), nullable=False)
    limits_version: Mapped[str] = mapped_column(String(64), nullable=False)
    builtin_exclusions_version: Mapped[str] = mapped_column(String(64), nullable=False)
    priority_matcher_version: Mapped[str] = mapped_column(String(64), nullable=False)
    service_defaults_rule_version_id: Mapped[UUID] = mapped_column(
        ForeignKey("rule_versions.id"), nullable=False
    )
    defaults_checksum: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    prompt_bindings: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    execution_profile: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    policy_digest: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    severity_threshold: Mapped[SeverityThreshold | None] = mapped_column(
        pg_enum(SeverityThreshold, "severity_threshold"), nullable=True
    )
    max_comments: Mapped[int | None] = mapped_column(SMALLINT, nullable=True)
    created_at: Mapped[datetime] = timestamp_column()


class ContextPayload(Base):
    __tablename__ = "context_payloads"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    run_id: Mapped[UUID] = mapped_column(ForeignKey("runs.id"), nullable=False, unique=True)
    schema_version: Mapped[int] = mapped_column(INTEGER, nullable=False)
    summary: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    s3_ref: Mapped[str | None] = mapped_column(TEXT, nullable=True)
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
    inline_comment: Mapped[bool] = mapped_column(
        BOOLEAN, nullable=False, server_default=text("false")
    )
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


class RunActionResponseBody(Base):
    """A ``run_actions`` response over 64 KB, referenced by ``response_ref`` (D1)."""

    __tablename__ = "run_action_responses"
    __table_args__ = (Index("ix_run_action_responses_run_id", "run_id"),)

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    run_id: Mapped[UUID] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), nullable=False)
    body: Mapped[Any] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = timestamp_column()


class ContextManifest(Base):
    """The exact blocks one snapshot-path provider call was shown (CVS §5, #130 F1-6).

    Written before the call; a retry reuses the row, a repair or a fallback has its own.
    """

    __tablename__ = "context_manifests"
    __table_args__ = (
        CheckConstraint(
            "stage IN ('conventions', 'review', 'verification')",
            name="ck_context_manifests_stage",
        ),
        CheckConstraint(
            "recovery_kind IN ('none', 'retry', 'repair', 'fallback')",
            name="ck_context_manifests_recovery_kind",
        ),
        Index("ix_context_manifests_run_attempt", "run_id", "attempt"),
    )

    # The manifest_id of the llm.call and context.manifest records.
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    run_id: Mapped[UUID] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), nullable=False)
    attempt: Mapped[int] = mapped_column(INTEGER, nullable=False)
    call_id: Mapped[UUID] = mapped_column(Uuid, nullable=False, unique=True)
    stage: Mapped[str] = mapped_column(String(20), nullable=False)
    recovery_kind: Mapped[str] = mapped_column(String(20), nullable=False)
    parent_call_id: Mapped[UUID | None] = mapped_column(Uuid, nullable=True)
    # CVS §8.3: the manifest names the snapshot of its run; it goes with it.
    policy_snapshot_id: Mapped[UUID] = mapped_column(
        ForeignKey("run_policy_snapshots.snapshot_id", ondelete="CASCADE"), nullable=False
    )
    # SHA-256 of the canonical JSON of ``blocks``.
    digest: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    # CVS §5.1, known before the call: SHA-256 of the canonical JSON of the exact provider
    # messages, the checksum of the prompt the stage pinned, and SHA-256 of the canonical JSON
    # of the pinned execution profile.
    rendered_messages_sha256: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    prompt_checksum: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    model_profile_version: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    block_count: Mapped[int] = mapped_column(INTEGER, nullable=False)
    input_tokens: Mapped[int] = mapped_column(INTEGER, nullable=False)
    blocks: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False)
    omissions: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    schema_version: Mapped[int] = mapped_column(INTEGER, nullable=False, server_default=text("1"))
    created_at: Mapped[datetime] = timestamp_column()
