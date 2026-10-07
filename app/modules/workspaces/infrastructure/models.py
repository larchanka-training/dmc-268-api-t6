"""SQLAlchemy 2 persistence models for the initial PostgreSQL schema.

These are deliberately separate from HTTP, messaging and domain DTOs.  Their table
and column names are the stable persistence contract used by Alembic.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import (
    BIGINT,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Numeric,
    String,
    Uuid,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.common.infrastructure.db.base import Base
from app.common.infrastructure.db.columns import timestamp_column


class Workspace(Base):
    __tablename__ = "workspaces"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    daily_budget_usd: Mapped[Decimal] = mapped_column(Numeric(12, 6), nullable=False)
    created_at: Mapped[datetime] = timestamp_column()
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class GitHubUserWorkspaceAccess(Base):
    """Workspace access granted by a verified GitHub user installation listing."""

    __tablename__ = "github_user_workspace_access"
    __table_args__ = (
        CheckConstraint("github_user_id > 0", name="ck_github_user_workspace_access_user_positive"),
        Index("ix_github_user_workspace_access_workspace_id", "workspace_id"),
    )

    github_user_id: Mapped[int] = mapped_column(BIGINT, primary_key=True)
    workspace_id: Mapped[UUID] = mapped_column(ForeignKey("workspaces.id"), primary_key=True)
    created_at: Mapped[datetime] = timestamp_column()


class GitHubUserRepositoryAccess(Base):
    """User-token-visible repository IDs under a linked installation Workspace."""

    __tablename__ = "github_user_repository_access"
    __table_args__ = (
        CheckConstraint(
            "github_user_id > 0", name="ck_github_user_repository_access_user_positive"
        ),
        CheckConstraint(
            "repository_external_id > 0",
            name="ck_github_user_repository_access_repository_positive",
        ),
        Index(
            "ix_github_user_repository_access_installation_repository",
            "provider_installation_id",
            "repository_external_id",
        ),
    )

    github_user_id: Mapped[int] = mapped_column(BIGINT, primary_key=True)
    provider_installation_id: Mapped[UUID] = mapped_column(
        ForeignKey("provider_installations.id"), primary_key=True
    )
    repository_external_id: Mapped[int] = mapped_column(BIGINT, primary_key=True)
    created_at: Mapped[datetime] = timestamp_column()


class GitHubUserInstallationSync(Base):
    """Monotonic snapshot generations for one authenticated GitHub user."""

    __tablename__ = "github_user_installation_sync"
    __table_args__ = (
        CheckConstraint(
            "github_user_id > 0", name="ck_github_user_installation_sync_user_positive"
        ),
        CheckConstraint(
            "reserved_generation >= applied_generation AND applied_generation >= 0",
            name="ck_github_user_installation_sync_generation_order",
        ),
    )

    github_user_id: Mapped[int] = mapped_column(BIGINT, primary_key=True)
    reserved_generation: Mapped[int] = mapped_column(BIGINT, nullable=False)
    applied_generation: Mapped[int] = mapped_column(BIGINT, nullable=False)


class GitHubInstallationAccessRevocation(Base):
    """Latest App-side revocation; external id zero covers the whole installation."""

    __tablename__ = "github_installation_access_revocations"
    __table_args__ = (
        CheckConstraint(
            "repository_external_id >= 0",
            name="ck_github_installation_access_revocations_repo_nonnegative",
        ),
    )

    provider_installation_id: Mapped[UUID] = mapped_column(
        ForeignKey("provider_installations.id"), primary_key=True
    )
    repository_external_id: Mapped[int] = mapped_column(BIGINT, primary_key=True)
    revoked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
