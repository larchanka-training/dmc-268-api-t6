"""Fake Unit of Work wrappers: use cases write only inside a UoW (backend.md §4)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from types import TracebackType
from typing import Any


@dataclass
class FakeUow:
    """Exposes one fake repository under ``attribute`` and records commits."""

    attribute: str
    port: Any
    commits: int = 0
    rollbacks: int = 0
    _extra: dict[str, Any] = field(default_factory=dict)

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
        self.commits += 1

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
class ProcessingUow:
    """Run-processing UoW whose blob writes become visible in ``cache`` only on commit."""

    repository: Any
    cache: Any | None = None
    commits: int = 0
    pending: list[tuple[Any, str, Any]] = field(default_factory=list)

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
        if self.cache is not None:
            for key, content, ttl in self.pending:
                await self.cache.put(key, content, ttl=ttl)
        self.pending.clear()

    async def rollback(self) -> None:
        self.pending.clear()


def processing_uow(repository: Any, cache: Any | None = None) -> Callable[[], Any]:
    return lambda: ProcessingUow(repository, cache)
