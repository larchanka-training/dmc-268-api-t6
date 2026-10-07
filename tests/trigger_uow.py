"""Fake Unit of Work wrappers: use cases write only inside a UoW (backend.md §4)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from types import TracebackType
from typing import Any


@dataclass
class FakeUow:
    """Exposes one fake repository under ``attribute``."""

    attribute: str
    port: Any
    rollbacks: int = 0

    def __getattr__(self, name: str) -> Any:
        if name == self.attribute:
            return self.port
        raise AttributeError(name)

    async def __aenter__(self) -> FakeUow:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        return None

    async def commit(self) -> None:
        pass

    async def rollback(self) -> None:
        self.rollbacks += 1


def targets_uow(targets: Any) -> Callable[[], Any]:
    return lambda: FakeUow("targets", targets)


def candidates_uow(candidates: Any) -> Callable[[], Any]:
    return lambda: FakeUow("candidates", candidates)


@dataclass
class _StagedCache:
    uow: ProcessingUow

    async def put(self, key: Any, content: str, *, ttl: Any) -> None:
        self.uow.pending.append((key, content, ttl))


@dataclass
class _StagedRepository:
    uow: ProcessingUow
    target: Any

    def __getattr__(self, name: str) -> Any:
        return getattr(self.target, name)

    async def store_diff_snapshots(
        self,
        run_id: Any,
        code_change_id: Any,
        head_sha: Any,
        snapshots: Any,
    ) -> Any:
        self.uow.pending_snapshots.append((run_id, code_change_id, head_sha, snapshots))
        return snapshots


class ProcessingUow:
    """Run-processing UoW whose diff snapshots and blob writes become visible only on commit."""

    def __init__(self, repository: Any, cache: Any | None = None) -> None:
        self._target_repository = repository
        self.cache = cache
        self.commits: int = 0
        self.pending: list[tuple[Any, str, Any]] = []
        self.pending_snapshots: list[tuple[Any, Any, Any, Any]] = []

    @property
    def repository(self) -> _StagedRepository:
        return _StagedRepository(self, self._target_repository)

    @property
    def blob_cache(self) -> _StagedCache | None:
        return None if self.cache is None else _StagedCache(self)

    async def __aenter__(self) -> ProcessingUow:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        return None

    async def commit(self) -> None:
        self.commits += 1
        for run_id, code_change_id, head_sha, snapshots in self.pending_snapshots:
            await self._target_repository.store_diff_snapshots(
                run_id, code_change_id, head_sha, snapshots
            )
        self.pending_snapshots.clear()
        if self.cache is not None:
            for key, content, ttl in self.pending:
                await self.cache.put(key, content, ttl=ttl)
        self.pending.clear()

    async def rollback(self) -> None:
        self.pending_snapshots.clear()
        self.pending.clear()


def processing_uow(repository: Any, cache: Any | None = None) -> Callable[[], Any]:
    return lambda: ProcessingUow(repository, cache)
