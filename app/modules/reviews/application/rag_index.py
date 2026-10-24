"""The RAG index a review opens before it builds its context (#144, ADR 0001 §6.1)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from app.common.application.unit_of_work import UnitOfWork
from app.modules.code_index.domain.rag_index import RagIndex

# The pinned review prompts, as `(key, version)`, that have a retrieved section (M6). Empty
# until v5: a usable index is then recorded as `not_ready` / `prompt_version`.
RETRIEVED_PROMPT_VERSIONS: frozenset[tuple[str, str]] = frozenset()


def has_retrieved_section(review_system_key: str, review_system_version: str) -> bool:
    """Whether the pinned review prompt of a run shows retrieved blocks."""
    return (review_system_key, review_system_version) in RETRIEVED_PROMPT_VERSIONS


@dataclass(frozen=True)
class RagReadiness:
    """The index as the review found it; ``index`` is None when RAG is not in effect.

    ``default_head_sha`` is the index target, the default-branch head it is built for.
    """

    enabled: bool
    index: RagIndex | None = None
    default_head_sha: str | None = None


class ReviewIndexAccess(Protocol):
    """Works in the caller's transaction: flushes, never commits, and sets a
    transaction-local `lock_timeout` in it."""

    async def open_for_review(self, repository_id: UUID) -> RagReadiness:
        """Read the index of a repository for a review, requesting a build or extending
        its use when the index needs it; never waits for a build."""
        ...


class ReviewIndexUnitOfWork(UnitOfWork, Protocol):
    @property
    def review_index(self) -> ReviewIndexAccess: ...


type ReviewIndexUowFactory = Callable[[], ReviewIndexUnitOfWork]


class OpenReviewIndex:
    def __init__(self, uow_factory: ReviewIndexUowFactory) -> None:
        self._uow_factory = uow_factory

    async def execute(self, repository_id: UUID) -> RagReadiness:
        async with self._uow_factory() as uow:
            readiness = await uow.review_index.open_for_review(repository_id)
            await uow.commit()
            return readiness
