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
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.common.infrastructure.db.base import Base
from app.common.infrastructure.db.columns import timestamp_column


class WebhookEvent(Base):
    __tablename__ = "webhook_events"
    __table_args__ = (
        CheckConstraint(
            "payload IS NOT NULL OR payload_s3_ref IS NOT NULL",
            name="ck_webhook_events_payload_present",
        ),
        CheckConstraint(
            "(projection_claim_token IS NULL) = (projection_lease_until IS NULL)",
            name="ck_webhook_events_projection_claim_pair",
        ),
        Index(
            "ix_webhook_events_pending_retry",
            "retry_after",
            "received_at",
            postgresql_where=text("projected_at IS NULL AND payload IS NOT NULL"),
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    provider_installation_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("provider_installations.id"), nullable=True
    )
    installation_external_id: Mapped[int | None] = mapped_column(BIGINT, nullable=True)
    delivery_id: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    event: Mapped[str] = mapped_column(String(100), nullable=False)
    action: Mapped[str | None] = mapped_column(String(100))
    payload_s3_ref: Mapped[str | None] = mapped_column(TEXT, nullable=True)
    payload: Mapped[dict[str, object] | None] = mapped_column(JSONB, nullable=True)
    projection_claim_token: Mapped[UUID | None] = mapped_column(Uuid, nullable=True)
    projection_lease_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    retry_after: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    projected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    received_at: Mapped[datetime] = timestamp_column()
