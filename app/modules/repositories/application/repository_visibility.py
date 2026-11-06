"""Keep the saved visibility of a GitHub repository current (#454).

The public badge trusts this flag alone. Every write says when the flag was known, and only a
newer observation replaces the stored one; one short transaction, no GitHub call inside it.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol

from app.common.application.unit_of_work import UnitOfWork

_LOGGER = logging.getLogger(__name__)

# A flag not confirmed for this long reads as not public, recorded in contracts/openapi.yaml.
VISIBILITY_MAX_AGE = timedelta(hours=72)
# The background re-read confirms a flag older than this, well before it expires: a failed pass
# is retried an hour later, so GitHub or the worker can be away for two days unnoticed.
VISIBILITY_REFRESH_AFTER = timedelta(hours=24)
# One pass an hour per worker: up to 1200 repositories a day.
VISIBILITY_REFRESH_BATCH = 50
# So that a GitHub outage holds the webhook scan for a few timeouts, not for a whole batch.
_MAX_FAILED_READS = 3


@dataclass(frozen=True)
class RepositoryVisibilityTarget:
    installation_external_id: int
    repository_external_id: int


@dataclass(frozen=True)
class RepositoryVisibilityDue:
    """Rows to read, and the database time taken before any of the reads."""

    observed_at: datetime
    targets: tuple[RepositoryVisibilityTarget, ...]


@dataclass(frozen=True)
class RepositoryVisibilityReading:
    """What GitHub says now; None in a field it did not answer in a usable form."""

    private: bool | None
    full_name: str | None
    web_url: str | None


class RepositoryVisibilityUnavailable(Exception):
    """GitHub could not answer right now, which says nothing about the repository."""


class RepositoryVisibilityReader(Protocol):
    async def read(
        self, installation_external_id: int, repository_external_id: int
    ) -> RepositoryVisibilityReading | None:
        """``None`` when GitHub does not show the repository to the App any more."""
        ...


class RepositoryVisibilityStore(Protocol):
    async def due(self, *, older_than: timedelta, limit: int) -> RepositoryVisibilityDue:
        """GitHub repositories of live installations, never observed or longest ago first."""
        ...

    async def forget(self, installation_external_id: int, observed_at: datetime) -> bool:
        """Back to unknown for every repository of the installation; False when it has none."""
        ...

    async def forget_repository(self, repository_external_id: int, observed_at: datetime) -> bool:
        """Back to unknown wherever a GitHub installation holds it; False when none does."""
        ...

    async def rename(
        self,
        installation_external_id: int,
        repository_external_id: int,
        full_name: str,
        web_url: str | None,
        observed_at: datetime,
    ) -> bool:
        """False when the repository is not connected."""
        ...

    async def overwrite(
        self,
        installation_external_id: int,
        repository_external_id: int,
        private: bool | None,
        observed_at: datetime,
    ) -> bool:
        """False when the repository is not connected."""
        ...

    async def learn(
        self,
        installation_external_id: int,
        repository_external_id: int,
        private: bool,
        observed_at: datetime,
    ) -> None:
        """Fill an unknown value or close; ``overwrite`` alone opens."""
        ...


class RepositoryVisibilityUnitOfWork(UnitOfWork, Protocol):
    @property
    def visibility(self) -> RepositoryVisibilityStore: ...


class RecordRepositoryVisibility:
    def __init__(self, uow_factory: Callable[[], RepositoryVisibilityUnitOfWork]) -> None:
        self._uow_factory = uow_factory

    async def forget(self, installation_external_id: int, observed_at: datetime) -> bool:
        """``installation.suspend``: no event reaches the service until it ends, so nothing would
        say a repository went private. A delivery older than the suspension cannot fill it back."""
        async with self._uow_factory() as uow:
            known = await uow.visibility.forget(installation_external_id, observed_at)
            await uow.commit()
        return known

    async def forget_repository(self, repository_external_id: int, observed_at: datetime) -> bool:
        """``repository.transferred``: a row is never moved to the new owner's installation."""
        async with self._uow_factory() as uow:
            known = await uow.visibility.forget_repository(repository_external_id, observed_at)
            await uow.commit()
        return known

    async def rename(
        self,
        installation_external_id: int,
        repository_external_id: int,
        full_name: str,
        web_url: str | None,
        observed_at: datetime,
    ) -> bool:
        """``repository.renamed``: the badge finds a repository by its current name."""
        async with self._uow_factory() as uow:
            known = await uow.visibility.rename(
                installation_external_id, repository_external_id, full_name, web_url, observed_at
            )
            await uow.commit()
        return known

    async def overwrite(
        self,
        installation_external_id: int,
        repository_external_id: int,
        private: bool,
        observed_at: datetime,
    ) -> bool:
        """``repository.privatized`` or ``publicized``: the change itself, so it may open."""
        async with self._uow_factory() as uow:
            known = await uow.visibility.overwrite(
                installation_external_id, repository_external_id, private, observed_at
            )
            await uow.commit()
        return known

    async def learn(
        self,
        installation_external_id: int,
        repository_external_id: int,
        private: bool,
        observed_at: datetime,
    ) -> None:
        """The flag in the payload of another event: it fills the rows connected before the flag
        existed and may close, but never opens."""
        async with self._uow_factory() as uow:
            await uow.visibility.learn(
                installation_external_id, repository_external_id, private, observed_at
            )
            await uow.commit()


class RefreshRepositoryVisibility:
    """Confirm the flags no event has touched lately; the reads run outside any transaction.

    It keeps a quiet public repository on its badge, and it closes one whose ``privatized``
    delivery was lost. The load is bounded by the connected repositories per interval and does
    not depend on how often a badge is requested.
    """

    def __init__(
        self,
        *,
        uow_factory: Callable[[], RepositoryVisibilityUnitOfWork],
        reader: RepositoryVisibilityReader,
    ) -> None:
        self._uow_factory = uow_factory
        self._reader = reader

    async def execute(self) -> int:
        async with self._uow_factory() as uow:
            due = await uow.visibility.due(
                older_than=VISIBILITY_REFRESH_AFTER, limit=VISIBILITY_REFRESH_BATCH
            )
        refreshed = 0
        failed = 0
        for target in due.targets:
            try:
                reading = await self._reader.read(
                    target.installation_external_id, target.repository_external_id
                )
            except RepositoryVisibilityUnavailable as error:
                _LOGGER.warning(
                    "Repository visibility read failed for repository %s: %s",
                    target.repository_external_id,
                    error,
                )
            except Exception:
                _LOGGER.exception(
                    "Repository visibility read failed for repository %s",
                    target.repository_external_id,
                )
            else:
                await self._save(target, reading, due.observed_at)
                refreshed += 1
                continue
            failed += 1
            if failed == _MAX_FAILED_READS:
                break
        return refreshed

    async def _save(
        self,
        target: RepositoryVisibilityTarget,
        reading: RepositoryVisibilityReading | None,
        observed_at: datetime,
    ) -> None:
        async with self._uow_factory() as uow:
            await uow.visibility.overwrite(
                target.installation_external_id,
                target.repository_external_id,
                None if reading is None else reading.private,
                observed_at,
            )
            if reading is not None and reading.full_name is not None:
                await uow.visibility.rename(
                    target.installation_external_id,
                    target.repository_external_id,
                    reading.full_name,
                    reading.web_url,
                    observed_at,
                )
            await uow.commit()
