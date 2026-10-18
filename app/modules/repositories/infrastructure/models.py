"""SQLAlchemy 2 persistence models for the initial PostgreSQL schema.

These are deliberately separate from HTTP, messaging and domain DTOs.  Their table
and column names are the stable persistence contract used by Alembic.
"""

from __future__ import annotations

import json
from datetime import datetime
from hashlib import sha256
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
    String,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.common.application.branch_patterns import BRANCH_PATTERN_MAX_LENGTH
from app.common.application.github_repository_name import REPOSITORY_FULL_NAME_MAX_LENGTH
from app.common.infrastructure.db.base import Base
from app.common.infrastructure.db.columns import pg_enum, timestamp_column
from app.common.infrastructure.db.enums import (
    Engine,
    ReviewEvent,
    SeverityThreshold,
    WaitForCi,
)


class ProviderInstallation(Base):
    __tablename__ = "provider_installations"
    __table_args__ = (
        UniqueConstraint("provider", "external_id"),
        Index("ix_provider_installations_workspace_id", "workspace_id"),
        CheckConstraint(
            "account_external_id > 0", name="ck_provider_installations_account_positive"
        ),
        Index("ix_provider_installations_provider_account", "provider", "account_external_id"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    workspace_id: Mapped[UUID] = mapped_column(ForeignKey("workspaces.id"), nullable=False)
    provider: Mapped[str] = mapped_column(String(50), nullable=False)
    external_id: Mapped[int] = mapped_column(BIGINT, nullable=False)
    account_external_id: Mapped[int | None] = mapped_column(BIGINT, nullable=True)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    provider_metadata: Mapped[dict[str, Any]] = mapped_column("metadata", JSONB, nullable=False)
    created_at: Mapped[datetime] = timestamp_column()
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class Repository(Base):
    __tablename__ = "repositories"
    __table_args__ = (
        UniqueConstraint("provider_installation_id", "external_id"),
        CheckConstraint("max_comments > 0", name="ck_repositories_max_comments_positive"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    provider_installation_id: Mapped[UUID] = mapped_column(
        ForeignKey("provider_installations.id"), nullable=False
    )
    external_id: Mapped[int] = mapped_column(BIGINT, nullable=False)
    full_name: Mapped[str] = mapped_column(String(REPOSITORY_FULL_NAME_MAX_LENGTH), nullable=False)
    default_branch: Mapped[str] = mapped_column(String(255), nullable=False)
    web_url: Mapped[str] = mapped_column(TEXT, nullable=False)
    enabled: Mapped[bool] = mapped_column(BOOLEAN, nullable=False, server_default=text("true"))
    installation_available: Mapped[bool] = mapped_column(
        BOOLEAN, nullable=False, server_default=text("true")
    )
    # The RAG toggle (#145): off by default, and onboarding never turns it on.
    rag_enabled: Mapped[bool] = mapped_column(BOOLEAN, nullable=False, server_default=text("false"))
    default_engine: Mapped[Engine] = mapped_column(
        pg_enum(Engine, "engine"), nullable=False, server_default=Engine.FAST.value
    )
    prompt_version_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("prompt_versions.id"), nullable=True
    )
    wait_for_ci: Mapped[WaitForCi] = mapped_column(
        pg_enum(WaitForCi, "wait_for_ci"), nullable=False, server_default=WaitForCi.AUTO.value
    )
    review_event: Mapped[ReviewEvent] = mapped_column(
        pg_enum(ReviewEvent, "review_event"),
        nullable=False,
        server_default=ReviewEvent.COMMENT.value,
    )
    max_comments: Mapped[int] = mapped_column(SMALLINT, nullable=False, server_default=text("10"))
    # Target-branch patterns (SD §6.1); the empty default selects every branch.
    branch_filter: Mapped[list[str]] = mapped_column(
        ARRAY(String(BRANCH_PATTERN_MAX_LENGTH)), nullable=False, server_default=text("'{}'")
    )
    severity_threshold: Mapped[SeverityThreshold] = mapped_column(
        pg_enum(SeverityThreshold, "severity_threshold"),
        nullable=False,
        server_default=SeverityThreshold.ALL.value,
    )
    created_at: Mapped[datetime] = timestamp_column()
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class RuleVersion(Base):
    __tablename__ = "rule_versions"
    __table_args__ = (
        UniqueConstraint("repository_id", "version"),
        CheckConstraint("version > 0", name="ck_rule_versions_version_positive"),
        CheckConstraint("checksum ~ '^[0-9a-f]{64}$'", name="ck_rule_versions_checksum_sha256"),
        Index(
            "uq_rule_versions_active_repository",
            "repository_id",
            unique=True,
            postgresql_where=text("is_active"),
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    repository_id: Mapped[UUID] = mapped_column(ForeignKey("repositories.id"), nullable=False)
    version: Mapped[int] = mapped_column(INTEGER, nullable=False)
    rules: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False)
    checksum: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    is_active: Mapped[bool] = mapped_column(BOOLEAN, nullable=False, server_default=text("true"))
    created_at: Mapped[datetime] = timestamp_column()

    @classmethod
    def from_rules(
        cls,
        *,
        repository_id: UUID,
        version: int,
        rules: list[dict[str, Any]],
        is_active: bool = True,
    ) -> RuleVersion:
        """Build a persistence row from schema-validated rules.

        Schema validation and stack selection belong to the onboarding use case;
        this factory preserves the rule array and derives its stable checksum.
        """
        if version <= 0:
            raise ValueError("rule version must be positive")
        canonical_rules = json.dumps(rules, sort_keys=True, separators=(",", ":"))
        return cls(
            repository_id=repository_id,
            version=version,
            rules=rules,
            checksum=sha256(canonical_rules.encode("utf-8")).hexdigest(),
            is_active=is_active,
        )


class RepoConventions(Base):
    __tablename__ = "repo_conventions"
    __table_args__ = (
        UniqueConstraint(
            "repository_id",
            "agents_md_sha",
            "prompt_version_id",
            "policy_key",
            name="uq_repo_conventions_snapshot",
            postgresql_nulls_not_distinct=True,
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    repository_id: Mapped[UUID] = mapped_column(ForeignKey("repositories.id"), nullable=False)
    agents_md_sha: Mapped[str | None] = mapped_column(String(64))
    prompt_version_id: Mapped[UUID] = mapped_column(
        ForeignKey("prompt_versions.id"), nullable=False
    )
    # Digest of the ignore policy the entry was generated under; NULL for entries made
    # without one, which a run with a policy never reuses.
    policy_key: Mapped[str | None] = mapped_column(CHAR(64))
    agents_md: Mapped[str | None] = mapped_column(TEXT)
    key_patterns: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    recommendations: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    languages: Mapped[dict[str, int]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = timestamp_column()


class RepoConventionDraft(Base):
    """Recoverable per-cache-entry model draft kept outside the conventions value."""

    __tablename__ = "repo_convention_drafts"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    repo_conventions_id: Mapped[UUID] = mapped_column(
        ForeignKey("repo_conventions.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    files: Mapped[list[dict[str, str]]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = timestamp_column()


class RepositoryRulesStatus(Base):
    """The last read of ``.review/rules.md`` on the default branch (#150), one row per repository.

    ``webhook-worker`` writes it; the API only reads it and requests a new read. A row
    without ``status`` is a request that has not been answered yet. ``missing`` is a
    stored result too: the absence was proved by a complete read of the revision.
    """

    __tablename__ = "repository_rules_status"
    __table_args__ = (
        CheckConstraint(
            "status IN ('present', 'missing', 'invalid', 'unavailable')",
            name="ck_repository_rules_status_status",
        ),
        # A read records its branch and time; a request that is not answered yet has neither.
        CheckConstraint(
            "num_nulls(status, branch, checked_at) IN (0, 3)",
            name="ck_repository_rules_status_read_complete",
        ),
        CheckConstraint(
            "sha IS NULL OR sha ~ '^[0-9a-f]{40}$'",
            name="ck_repository_rules_status_sha",
        ),
        CheckConstraint(
            "checksum IS NULL OR checksum ~ '^[0-9a-f]{64}$'",
            name="ck_repository_rules_status_checksum",
        ),
        CheckConstraint(
            "status <> 'present' OR num_nulls(sha, text, checksum, ignore_masks) = 0",
            name="ck_repository_rules_status_present",
        ),
        CheckConstraint(
            "status <> 'missing' OR num_nulls(sha, defaults_version) = 0",
            name="ck_repository_rules_status_missing",
        ),
        CheckConstraint(
            "status NOT IN ('invalid', 'unavailable') OR jsonb_array_length(diagnostics) > 0",
            name="ck_repository_rules_status_diagnosed",
        ),
        Index(
            "ix_repository_rules_status_refresh",
            "refresh_requested_at",
            postgresql_where=text("refresh_requested_at IS NOT NULL"),
        ),
    )

    repository_id: Mapped[UUID] = mapped_column(
        ForeignKey("repositories.id", ondelete="CASCADE"), primary_key=True
    )
    status: Mapped[str | None] = mapped_column(String(16))
    branch: Mapped[str | None] = mapped_column(String(255))
    sha: Mapped[str | None] = mapped_column(String(40))
    # ``text`` would shadow ``sqlalchemy.text`` in the class body.
    content: Mapped[str | None] = mapped_column("text", TEXT)
    # SHA-256 of the raw bytes (RULES_FORMAT_SPEC §3.2); the Git blob id is not a checksum.
    checksum: Mapped[str | None] = mapped_column(CHAR(64))
    ignore_masks: Mapped[list[str] | None] = mapped_column(JSONB)
    diagnostics: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'")
    )
    defaults_version: Mapped[str | None] = mapped_column(String(64))
    checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # The durable re-read request: set by the API, a push or onboarding, cleared by the read
    # that answers it. The claim keeps two workers from reading the same repository at once.
    refresh_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    refresh_claimed_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
