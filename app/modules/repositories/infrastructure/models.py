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
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.common.infrastructure.db.base import Base
from app.common.infrastructure.db.columns import pg_enum, timestamp_column
from app.common.infrastructure.db.enums import (
    Engine,
    ReviewEvent,
    WaitForCi,
)


class ProviderInstallation(Base):
    __tablename__ = "provider_installations"
    __table_args__ = (
        UniqueConstraint("provider", "external_id"),
        Index("ix_provider_installations_workspace_id", "workspace_id"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    workspace_id: Mapped[UUID] = mapped_column(ForeignKey("workspaces.id"), nullable=False)
    provider: Mapped[str] = mapped_column(String(50), nullable=False)
    external_id: Mapped[int] = mapped_column(BIGINT, nullable=False)
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
    full_name: Mapped[str] = mapped_column(String(512), nullable=False)
    default_branch: Mapped[str] = mapped_column(String(255), nullable=False)
    web_url: Mapped[str] = mapped_column(TEXT, nullable=False)
    enabled: Mapped[bool] = mapped_column(BOOLEAN, nullable=False, server_default=text("true"))
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
    agents_md: Mapped[str | None] = mapped_column(TEXT)
    key_patterns: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    recommendations: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    languages: Mapped[dict[str, int]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = timestamp_column()
