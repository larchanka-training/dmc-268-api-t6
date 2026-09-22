"""SQLAlchemy 2 persistence models for the initial PostgreSQL schema.

These are deliberately separate from HTTP, messaging and domain DTOs.  Their table
and column names are the stable persistence contract used by Alembic.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    BIGINT,
    TEXT,
    ForeignKey,
    String,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column

from database.base import Base
from database.columns import timestamp_column


class WebhookEvent(Base):
    __tablename__ = "webhook_events"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    provider_installation_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("provider_installations.id"), nullable=True
    )
    installation_external_id: Mapped[int] = mapped_column(BIGINT, nullable=False)
    delivery_id: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    event: Mapped[str] = mapped_column(String(100), nullable=False)
    action: Mapped[str | None] = mapped_column(String(100))
    payload_s3_ref: Mapped[str] = mapped_column(TEXT, nullable=False)
    received_at: Mapped[datetime] = timestamp_column()
