"""A webhook trigger's request for an index update (#143, ADR 0001 §6.1).

A merge or a push to the default branch writes the index target and the wake-up marker
in one short transaction; the worker leader wakes the job after the commit. The
delivery makes no GitHub call: the job reads the head and builds the change set from
the indexed commit, not from the pull request.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Protocol
from uuid import UUID

from app.modules.code_index.application.index_store import CodeIndexUnitOfWork

_LOGGER = logging.getLogger(__name__)


class IndexRefreshRepositories(Protocol):
    async def resolve(
        self, installation_external_id: int, repository_external_id: int
    ) -> UUID | None:
        """The repository of a GitHub delivery while its effective RAG is on; None for a
        repository that is not connected, has RAG off, is unavailable or whose
        installation is deleted."""
        ...


class IndexRefreshUnitOfWork(CodeIndexUnitOfWork, Protocol):
    @property
    def repositories(self) -> IndexRefreshRepositories: ...


class RequestIndexRefresh:
    """Request a build up to `commit_sha`, or only the wake-up marker when it is None.

    Returns whether the request was written: False for a repository the delivery does
    not resolve, and for a missing, purged or `failed` index. A repeat is harmless: it
    rewrites the same target and refreshes the marker, and jobs of one repository merge
    into one build up to the last target.
    """

    def __init__(self, uow: Callable[[], IndexRefreshUnitOfWork]) -> None:
        self._uow = uow

    async def execute(
        self, installation_external_id: int, repository_external_id: int, commit_sha: str | None
    ) -> bool:
        async with self._uow() as uow:
            repository_id = await uow.repositories.resolve(
                installation_external_id, repository_external_id
            )
            if repository_id is None:
                return False
            requested = await uow.index.request_build(repository_id, target_commit_sha=commit_sha)
            await uow.commit()
        if not requested:
            # SD §13: the internal id only, no customer data.
            _LOGGER.debug(
                "The code index of %s is missing, purged or failed: no update requested",
                repository_id,
            )
        return requested
