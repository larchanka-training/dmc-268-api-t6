"""Reusable persistence column and enum declarations."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from sqlalchemy import DateTime, Enum, func
from sqlalchemy.orm import Mapped, mapped_column


def timestamp_column() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


def pg_enum(enum_class: type[StrEnum], name: str) -> Enum:
    return Enum(enum_class, name=name, values_callable=lambda cls: [member.value for member in cls])
