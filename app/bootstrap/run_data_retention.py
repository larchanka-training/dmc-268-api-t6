"""The retention of run data on the worker leader (#457, SD §10): its period and its task."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import timedelta
from functools import partial

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.modules.reviews.application.run_data_retention import PurgeExpiredRunData
from app.modules.reviews.infrastructure.run_data_retention import (
    SqlAlchemyRunDataRetentionUnitOfWork,
)

_VARIABLE = "RUN_DATA_RETENTION_DAYS"
# At most ten years: the purge threshold stays far inside the timestamp range.
MAX_RETENTION_DAYS = 3_650
# Far above the limit; it keeps `int()` off a pathological value.
_MAX_DIGITS = 18


@dataclass(frozen=True)
class RunDataRetentionSettings:
    """`RUN_DATA_RETENTION_DAYS`, counted from `runs.finished_at`."""

    days: int = 90

    def __post_init__(self) -> None:
        if not 1 <= self.days <= MAX_RETENTION_DAYS:
            raise ValueError(f"{_VARIABLE} must be from 1 to {MAX_RETENTION_DAYS}")

    @classmethod
    def from_environment(cls, env: Mapping[str, str]) -> RunDataRetentionSettings:
        """An unset or empty variable keeps the default."""
        raw = env.get(_VARIABLE)
        if not raw:
            return cls()
        text = raw.strip()
        if not (text.isascii() and text.isdecimal()) or len(text) > _MAX_DIGITS:
            raise ValueError(f"{_VARIABLE} must be a positive integer")
        return cls(days=int(text))


def run_data_retention_task(
    settings: RunDataRetentionSettings, session_factory: async_sessionmaker[AsyncSession]
) -> PurgeExpiredRunData:
    return PurgeExpiredRunData(
        uow=partial(SqlAlchemyRunDataRetentionUnitOfWork, session_factory),
        retention=timedelta(days=settings.days),
    )
