"""SQLAlchemy 2 persistence models for the initial PostgreSQL schema.

These are deliberately separate from HTTP, messaging and domain DTOs.  Their table
and column names are the stable persistence contract used by Alembic.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import (
    DateTime,
    Numeric,
    String,
    Uuid,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from database.base import Base
from database.columns import timestamp_column


class Workspace(Base):
    __tablename__ = "workspaces"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    daily_budget_usd: Mapped[Decimal] = mapped_column(Numeric(12, 6), nullable=False)
    created_at: Mapped[datetime] = timestamp_column()
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
