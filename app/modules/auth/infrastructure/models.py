"""Durable GitHub user profiles and hashed local refresh sessions."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import BIGINT, CHAR, CheckConstraint, DateTime, ForeignKey, Index, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.common.infrastructure.db.base import Base
from app.common.infrastructure.db.columns import timestamp_column


class GitHubUserProfile(Base):
    __tablename__ = "github_user_profiles"
    __table_args__ = (CheckConstraint("id > 0", name="ck_github_user_profiles_id_positive"),)

    id: Mapped[int] = mapped_column(BIGINT, primary_key=True)
    login: Mapped[str] = mapped_column(String(255), nullable=False)
    name: Mapped[str | None] = mapped_column(String(255))
    avatar_url: Mapped[str | None] = mapped_column(String(2048))
    created_at: Mapped[datetime] = timestamp_column()
    updated_at: Mapped[datetime] = timestamp_column()


class AuthRefreshFamily(Base):
    __tablename__ = "auth_refresh_families"
    __table_args__ = (Index("ix_auth_refresh_families_user_id", "github_user_id"),)

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    github_user_id: Mapped[int] = mapped_column(
        BIGINT, ForeignKey("github_user_profiles.id"), nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = timestamp_column()


class AuthRefreshSession(Base):
    __tablename__ = "auth_refresh_sessions"
    __table_args__ = (
        Index("ix_auth_refresh_sessions_family_id", "family_id"),
        Index("ix_auth_refresh_sessions_user_id", "github_user_id"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    family_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("auth_refresh_families.id"), nullable=False
    )
    github_user_id: Mapped[int] = mapped_column(
        BIGINT, ForeignKey("github_user_profiles.id"), nullable=False
    )
    token_hash: Mapped[str] = mapped_column(CHAR(64), unique=True, nullable=False)
    created_at: Mapped[datetime] = timestamp_column()
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    rotated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
