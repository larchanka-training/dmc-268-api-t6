"""Bitbucket push index requests commit only through the use case."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import timedelta
from typing import cast
from uuid import UUID

import httpx
import pytest

from app.bootstrap.code_index_worker import CodeIndexSettings, index_versions
from app.modules.code_index.application.build_index import BuildRepositoryIndex, BuildStatus
from app.modules.code_index.application.index_store import (
    CodeIndexUnitOfWork,
    IndexAdmission,
    IndexBuildStats,
    IndexFence,
    LeaseClaim,
)
from app.modules.code_index.application.ports import (
    IndexedRepository,
    IndexRepositoryReader,
    IndexWakeups,
)
from app.modules.code_index.application.request_bitbucket_index_refresh import (
    RequestBitbucketIndexRefresh,
)
from app.modules.code_index.application.vcs import RepositorySnapshotSource
from app.modules.code_index.infrastructure.bitbucket_snapshot_source import (
    BitbucketRepositorySnapshotSource,
    BitbucketSourceBudgets,
)
from app.modules.code_index.infrastructure.tar_archive import TarArchiveReader
from app.modules.integrations.bitbucket.application.request_credentials import (
    BitbucketRequestCredential,
    BoundBitbucketTarget,
)
from app.modules.reviews.application.ast_symbols import SourceParser

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


@dataclass
class _BuildIndex:
    released: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    pre_source_delay: float = 0.0

    async def claim_lease(
        self, repository_id: UUID, generation: int, lease_seconds: int, admission: IndexAdmission
    ) -> LeaseClaim:
        assert repository_id == REPOSITORY
        return LeaseClaim(IndexFence(UUID(int=9), generation, 1), None)

    async def discard_abandoned(self, repository_id: UUID, fence: IndexFence, rows: int) -> int:
        if self.pre_source_delay:
            await asyncio.sleep(self.pre_source_delay)
        return 0

    async def mark_failed(
        self, repository_id: UUID, fence: IndexFence, error_code: str, *, stats: IndexBuildStats
    ) -> None:
        self.failed.append(error_code)

    async def release(
        self,
        repository_id: UUID,
        fence: IndexFence,
        *,
        last_error_code: str | None,
        stats: IndexBuildStats,
        terminal_head_sha: str | None = None,
        expected_target_sha: str | None = None,
    ) -> None:
        assert repository_id == REPOSITORY
        assert last_error_code is not None
        self.released.append(last_error_code)


@dataclass
class _BuildUow:
    index: _BuildIndex

    async def __aenter__(self) -> _BuildUow:
        return self

    async def __aexit__(self, *_: object) -> None:
        pass

    async def commit(self) -> None:
        pass


class _BuildReader:
    async def get(self, repository_id: UUID) -> IndexedRepository:
        return IndexedRepository(repository_id, "bitbucket", 1, "workspace/repo")


class _GitHubReader:
    async def get(self, repository_id: UUID) -> IndexedRepository:
        return IndexedRepository(repository_id, "github", 1, "owner/repo")


class _UncalledSource:
    request_count = 0

    async def head(self) -> None:
        raise AssertionError("the global deadline should stop the source call")


class _BuildCredentials:
    async def credential_for(self, target: BoundBitbucketTarget) -> BitbucketRequestCredential:
        return BitbucketRequestCredential(target, "test-token")

    async def checkpoint(self, target: BoundBitbucketTarget) -> None:
        pass


@pytest.mark.parametrize(
    ("response", "budgets", "code", "retry_after"),
    [
        (httpx.Response(429, headers={"Retry-After": "120"}), None, "vcs_rate_limited", 120),
        (
            httpx.Response(200, json={"uuid": "{11111111-1111-4111-8111-111111111111}"}),
            BitbucketSourceBudgets(read_bytes=1),
            "vcs_unavailable",
            60,
        ),
    ],
)
def test_bitbucket_quota_exhaustion_releases_index_instead_of_failing(
    response: httpx.Response,
    budgets: BitbucketSourceBudgets | None,
    code: str,
    retry_after: int,
) -> None:
    target = BoundBitbucketTarget(
        INSTALLATION,
        REPOSITORY,
        UUID(int=4),
        UUID("11111111-1111-4111-8111-111111111111"),
        1,
        1,
    )

    async def resolve_target() -> BoundBitbucketTarget:
        return target

    async def scenario() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: response)) as client:
            index = _BuildIndex()
            settings = CodeIndexSettings()

            def source(_: IndexedRepository) -> RepositorySnapshotSource:
                return BitbucketRepositorySnapshotSource(
                    client=client,
                    target_for_repository=resolve_target,
                    credentials=_BuildCredentials(),
                    limits=settings.limits,
                    budgets=budgets,
                )

            build = BuildRepositoryIndex(
                uow=cast(Callable[[], CodeIndexUnitOfWork], lambda: _BuildUow(index)),
                repositories=cast(IndexRepositoryReader, _BuildReader()),
                sources=source,
                archives=TarArchiveReader(settings.limits),
                parser=cast(SourceParser, object()),
                wakeups=cast(IndexWakeups, object()),
                versions=index_versions(settings.limits),
                limits=settings.limits,
                settings=settings.build,
            )
            outcome = await build.execute(REPOSITORY, 1)
            assert outcome.status is BuildStatus.TRANSIENT
            assert outcome.error_code == code
            assert outcome.retry_after == timedelta(seconds=retry_after)
            assert index.released == [code]

    asyncio.run(scenario())


def test_other_provider_global_deadline_keeps_limit_failure() -> None:
    async def scenario() -> None:
        index = _BuildIndex(pre_source_delay=2.05)
        settings = CodeIndexSettings(deadline_seconds=2, soft_cutoff_seconds=1)
        build = BuildRepositoryIndex(
            uow=cast(Callable[[], CodeIndexUnitOfWork], lambda: _BuildUow(index)),
            repositories=cast(IndexRepositoryReader, _GitHubReader()),
            sources=lambda _: cast(RepositorySnapshotSource, _UncalledSource()),
            archives=TarArchiveReader(settings.limits),
            parser=cast(SourceParser, object()),
            wakeups=cast(IndexWakeups, object()),
            versions=index_versions(settings.limits),
            limits=settings.limits,
            settings=settings.build,
        )

        outcome = await build.execute(REPOSITORY, 1)

        assert outcome.status is BuildStatus.FAILED
        assert outcome.error_code == "limit_exceeded:deadline"
        assert index.failed == ["limit_exceeded:deadline"]
        assert index.released == []

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("pre_source_delay", "provider_delay"),
    [(2.05, 0.0), (0.0, 2.05)],
    ids=["before-source", "during-source"],
)
def test_bitbucket_global_deadline_during_source_work_releases_index(
    pre_source_delay: float, provider_delay: float
) -> None:
    target = BoundBitbucketTarget(
        INSTALLATION,
        REPOSITORY,
        UUID(int=4),
        UUID("11111111-1111-4111-8111-111111111111"),
        1,
        1,
    )

    async def resolve_target() -> BoundBitbucketTarget:
        return target

    async def scenario() -> None:
        calls = 0

        async def handle(_: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            if provider_delay:
                await asyncio.sleep(provider_delay)
            return httpx.Response(200, json={"uuid": "{11111111-1111-4111-8111-111111111111}"})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            index = _BuildIndex(pre_source_delay=pre_source_delay)
            settings = CodeIndexSettings(
                deadline_seconds=2,
                soft_cutoff_seconds=1,
                bitbucket_deadline_seconds=1,
            )

            def source(_: IndexedRepository) -> RepositorySnapshotSource:
                return BitbucketRepositorySnapshotSource(
                    client=client,
                    target_for_repository=resolve_target,
                    credentials=_BuildCredentials(),
                    limits=settings.limits,
                    budgets=settings.bitbucket_budgets,
                )

            build = BuildRepositoryIndex(
                uow=cast(Callable[[], CodeIndexUnitOfWork], lambda: _BuildUow(index)),
                repositories=cast(IndexRepositoryReader, _BuildReader()),
                sources=source,
                archives=TarArchiveReader(settings.limits),
                parser=cast(SourceParser, object()),
                wakeups=cast(IndexWakeups, object()),
                versions=index_versions(settings.limits),
                limits=settings.limits,
                settings=settings.build,
            )
            outcome = await build.execute(REPOSITORY, 1)
            assert outcome.status is BuildStatus.TRANSIENT
            assert outcome.error_code == "vcs_unavailable"
            assert outcome.retry_after == timedelta(minutes=1)
            assert index.released == ["vcs_unavailable"]
            assert index.failed == []
            assert calls == (0 if pre_source_delay else 1)

    asyncio.run(scenario())
