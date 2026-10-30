"""Bitbucket push index requests commit only through the use case."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from uuid import UUID

from app.modules.code_index.application.request_bitbucket_index_refresh import (
    RequestBitbucketIndexRefresh,
)

INSTALLATION = UUID(int=1)
REPOSITORY = UUID(int=2)
REMOTE = UUID(int=3)


@dataclass
class _Repositories:
    resolved: UUID | None
    calls: list[str]

    async def resolve(
        self,
        installation_id: UUID,
        repository_id: UUID,
        repository_uuid: UUID,
        branches: frozenset[str],
        binding_generation: int,
        webhook_generation: int,
    ) -> UUID | None:
        assert (installation_id, repository_id, repository_uuid) == (
            INSTALLATION,
            REPOSITORY,
            REMOTE,
        )
        assert branches == frozenset({"main"})
        assert (binding_generation, webhook_generation) == (4, 5)
        self.calls.append("resolve")
        return self.resolved


@dataclass
class _Index:
    calls: list[str]

    async def request_build(self, repository_id: UUID, *, target_commit_sha: str | None) -> bool:
        assert repository_id == REPOSITORY and target_commit_sha is None
        self.calls.append("request")
        return True


@dataclass
class _Uow:
    repositories: _Repositories
    index: _Index
    calls: list[str] = field(default_factory=list)

    async def __aenter__(self) -> _Uow:
        self.calls.append("enter")
        return self

    async def __aexit__(self, *_: object) -> None:
        self.calls.append("exit")

    async def commit(self) -> None:
        self.calls.append("commit")

    async def rollback(self) -> None:
        self.calls.append("rollback")


def test_index_refresh_commits_after_request_only_for_active_binding() -> None:
    async def scenario() -> None:
        calls: list[str] = []
        repositories = _Repositories(REPOSITORY, calls)
        uow = _Uow(repositories, _Index(calls), calls)
        refresh = RequestBitbucketIndexRefresh(lambda: uow)
        assert await refresh.execute(INSTALLATION, REPOSITORY, REMOTE, frozenset({"main"}), 4, 5)
        assert calls == ["enter", "resolve", "request", "commit", "exit"]

        calls.clear()
        repositories.resolved = None
        assert not await refresh.execute(
            INSTALLATION, REPOSITORY, REMOTE, frozenset({"main"}), 4, 5
        )
        assert calls == ["enter", "resolve", "exit"]

    asyncio.run(scenario())
