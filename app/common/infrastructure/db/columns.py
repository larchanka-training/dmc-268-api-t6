"""Reusable persistence column and enum declarations."""

from datetime import datetime

from sqlalchemy import DateTime, Enum, func
from sqlalchemy.orm import Mapped, mapped_column

from app.common.infrastructure.db.enums import (
    CodeChangeState,
    Engine,
    LedgerKind,
    PaymentStatus,
    ReviewEvent,
    RunState,
    WaitForCi,
)


def timestamp_column() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


def pg_enum(
    enum_class: type[
        Engine | WaitForCi | ReviewEvent | CodeChangeState | RunState | PaymentStatus | LedgerKind
    ],
    name: str,
) -> Enum:
    return Enum(enum_class, name=name, values_callable=lambda cls: [member.value for member in cls])
