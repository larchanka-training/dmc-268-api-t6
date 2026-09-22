"""SQLAlchemy 2 persistence models for the initial PostgreSQL schema.

These are deliberately separate from HTTP, messaging and domain DTOs.  Their table
and column names are the stable persistence contract used by Alembic.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import (
    TEXT,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Numeric,
    String,
    Uuid,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from database.base import Base
from database.columns import pg_enum, timestamp_column
from database.enums import (
    LedgerKind,
    PaymentStatus,
)


class Payment(Base):
    __tablename__ = "payments"
    __table_args__ = (
        Index(
            "uq_payments_provider_checkout",
            "provider",
            "provider_checkout_id",
            unique=True,
            postgresql_where=text("provider_checkout_id IS NOT NULL"),
        ),
        Index(
            "uq_payments_provider_payment",
            "provider",
            "provider_payment_id",
            unique=True,
            postgresql_where=text("provider_payment_id IS NOT NULL"),
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    workspace_id: Mapped[UUID] = mapped_column(ForeignKey("workspaces.id"), nullable=False)
    provider: Mapped[str] = mapped_column(String(50), nullable=False)
    provider_checkout_id: Mapped[str | None] = mapped_column(String(255))
    provider_payment_id: Mapped[str | None] = mapped_column(String(255))
    amount_usd: Mapped[Decimal] = mapped_column(Numeric(12, 6), nullable=False)
    status: Mapped[PaymentStatus] = mapped_column(
        pg_enum(PaymentStatus, "payment_status"),
        nullable=False,
        server_default=PaymentStatus.PENDING.value,
    )
    created_at: Mapped[datetime] = timestamp_column()
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class CreditLedger(Base):
    __tablename__ = "credit_ledger"
    __table_args__ = (
        Index(
            "uq_credit_ledger_usage_event",
            "usage_event_id",
            unique=True,
            postgresql_where=text("usage_event_id IS NOT NULL"),
        ),
        Index(
            "uq_credit_ledger_payment",
            "payment_id",
            unique=True,
            postgresql_where=text("payment_id IS NOT NULL"),
        ),
        CheckConstraint(
            "kind != 'top_up' OR (amount_usd > 0 AND payment_id IS NOT NULL)",
            name="ck_credit_ledger_top_up",
        ),
        CheckConstraint(
            "kind != 'usage_debit' OR (amount_usd < 0 AND usage_event_id IS NOT NULL)",
            name="ck_credit_ledger_usage_debit",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    workspace_id: Mapped[UUID] = mapped_column(ForeignKey("workspaces.id"), nullable=False)
    kind: Mapped[LedgerKind] = mapped_column(pg_enum(LedgerKind, "ledger_kind"), nullable=False)
    amount_usd: Mapped[Decimal] = mapped_column(Numeric(12, 6), nullable=False)
    usage_event_id: Mapped[UUID | None] = mapped_column(ForeignKey("usage_events.id"))
    payment_id: Mapped[UUID | None] = mapped_column(ForeignKey("payments.id"))
    reason: Mapped[str | None] = mapped_column(TEXT)
    created_at: Mapped[datetime] = timestamp_column()
