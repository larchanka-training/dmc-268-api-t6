"""`UpdateRepository` against fakes of its ports: only `ragEnabled=false` purges (#143).

The order is the update, the purge with `disabled`, the re-read for the response, then
the commit; a body without `ragEnabled=false` purges nothing, and a repository the scoped
store cannot find is neither purged nor committed.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field, replace
from types import TracebackType
from typing import Self
from uuid import UUID

import pytest

from app.modules.code_index.domain.enums import PurgeReason, RagIndexStatus
from app.modules.code_index.domain.rag_index import RagIndex
from app.modules.repositories.application.repository_settings import (
    RepositorySettings,
    RepositorySettingsChange,
    UpdateRepository,
)

REPOSITORY_ID = UUID("00000000-0000-4000-8000-0000000000a2")
UPDATED = RepositorySettings(
    id=REPOSITORY_ID,
    full_name="octo/repo",
    url="https://github.com/octo/repo",
    default_branch="main",
    enabled=True,
    default_engine="fast",
    wait_for_ci="auto",
    max_comments=10,
    review_event="COMMENT",
    branch_filter=(),
    severity_threshold="all",
    installation_available=True,
    rag_enabled=False,
    rag_index=RagIndex(RagIndexStatus.DISABLED, None, None, 0, 0, None),
)
# The read after the purge differs from the update's result, so the test sees which one
# the use case returns.
REREAD = replace(UPDATED, full_name="octo/repo-after-purge")


@dataclass
class FakeRepositories:
    """The scoped store, the purger and the transaction, recording every call in order."""

    found: bool = True
    calls: list[str] = field(default_factory=list)

    @property
    def repositories(self) -> Self:
        return self

    @property
    def index_purger(self) -> Self:
        return self

    async def list_repositories(self) -> list[RepositorySettings]:
        raise AssertionError("UpdateRepository does not list repositories")

    async def get_repository(self, repository_id: UUID) -> RepositorySettings | None:
        self.calls.append(f"get {repository_id == REPOSITORY_ID}")
        return REREAD

    async def update_repository(
        self, repository_id: UUID, change: RepositorySettingsChange
    ) -> RepositorySettings | None:
        self.calls.append(f"update {repository_id == REPOSITORY_ID}")
        return UPDATED if self.found else None

    async def purge(self, repository_id: UUID, reason: PurgeReason) -> None:
        self.calls.append(f"purge {repository_id == REPOSITORY_ID} {reason}")

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        return None

    async def commit(self) -> None:
        self.calls.append("commit")

    async def rollback(self) -> None:
        self.calls.append("rollback")


def _execute(fake: FakeRepositories, change: RepositorySettingsChange) -> RepositorySettings | None:
    return asyncio.run(UpdateRepository(lambda: fake).execute(REPOSITORY_ID, change))


def test_rag_off_purges_with_disabled_rereads_and_then_commits() -> None:
    fake = FakeRepositories()

    item = _execute(fake, RepositorySettingsChange(rag_enabled=False))

    assert fake.calls == ["update True", "purge True disabled", "get True", "commit"]
    assert item == REREAD


@pytest.mark.parametrize(
    "change",
    [RepositorySettingsChange(enabled=False), RepositorySettingsChange(rag_enabled=True)],
    ids=["rag-enabled-absent", "rag-enabled-true"],
)
def test_a_body_without_rag_off_purges_nothing(change: RepositorySettingsChange) -> None:
    fake = FakeRepositories()

    item = _execute(fake, change)

    assert fake.calls == ["update True", "commit"]
    assert item == UPDATED


def test_a_missing_repository_is_neither_purged_nor_committed() -> None:
    fake = FakeRepositories(found=False)

    item = _execute(fake, RepositorySettingsChange(rag_enabled=False))

    assert fake.calls == ["update True"]
    assert item is None
