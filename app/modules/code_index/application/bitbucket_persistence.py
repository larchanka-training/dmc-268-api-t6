"""Short transactions for durable Bitbucket listing pages and published manifests."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from types import TracebackType
from typing import Protocol, Self
from uuid import UUID

from app.modules.code_index.application.vcs import ArchiveEntry


class BitbucketIndexTarget(Protocol):
    """Identity fields the index persistence boundary needs from a bound target."""

    @property
    def repository_id(self) -> UUID: ...

    @property
    def binding_generation(self) -> int: ...


@dataclass(frozen=True)
class SourceListingPage:
    target_sha: str
    directory: str
    params: dict[str, str]
    payload: dict[str, object]
    bytes_read: int
    attempts: int = 1
    complete: bool = True


class SourceListingCheckpoint(Protocol):
    async def load(
        self, target: BitbucketIndexTarget, sha: str
    ) -> tuple[SourceListingPage, ...]: ...

    async def save(self, target: BitbucketIndexTarget, page: SourceListingPage) -> None: ...

    async def charge(
        self, target: BitbucketIndexTarget, sha: str, directory: str, params: dict[str, str]
    ) -> None: ...

    async def add_bytes(
        self,
        target: BitbucketIndexTarget,
        sha: str,
        directory: str,
        params: dict[str, str],
        count: int,
    ) -> None: ...


class PublishedManifestStore(Protocol):
    async def load(
        self, target: BitbucketIndexTarget, base_sha: str
    ) -> dict[str, ArchiveEntry] | None: ...

    async def save(
        self, target: BitbucketIndexTarget, commit_sha: str, entries: Mapping[str, ArchiveEntry]
    ) -> bool: ...


class BitbucketPersistenceUnitOfWork(Protocol):
    @property
    def pages(self) -> SourceListingCheckpoint: ...

    @property
    def manifest(self) -> PublishedManifestStore: ...

    async def __aenter__(self) -> Self: ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None: ...

    async def commit(self) -> None: ...


class PersistBitbucketTreeCheckpoint:
    def __init__(self, uow: Callable[[], BitbucketPersistenceUnitOfWork]) -> None:
        self._uow = uow

    async def load(self, target: BitbucketIndexTarget, sha: str) -> tuple[SourceListingPage, ...]:
        async with self._uow() as uow:
            pages = await uow.pages.load(target, sha)
            # A stale checkpoint is deleted by load, so it may need a commit.
            await uow.commit()
            return pages

    async def charge(
        self, target: BitbucketIndexTarget, sha: str, directory: str, params: dict[str, str]
    ) -> None:
        async with self._uow() as uow:
            await uow.pages.charge(target, sha, directory, params)
            await uow.commit()

    async def save(self, target: BitbucketIndexTarget, page: SourceListingPage) -> None:
        async with self._uow() as uow:
            await uow.pages.save(target, page)
            await uow.commit()

    async def add_bytes(
        self,
        target: BitbucketIndexTarget,
        sha: str,
        directory: str,
        params: dict[str, str],
        count: int,
    ) -> None:
        async with self._uow() as uow:
            await uow.pages.add_bytes(target, sha, directory, params, count)
            await uow.commit()


class PersistBitbucketPublishedManifest:
    def __init__(self, uow: Callable[[], BitbucketPersistenceUnitOfWork]) -> None:
        self._uow = uow

    async def load(
        self, target: BitbucketIndexTarget, base_sha: str
    ) -> dict[str, ArchiveEntry] | None:
        async with self._uow() as uow:
            return await uow.manifest.load(target, base_sha)

    async def save(
        self, target: BitbucketIndexTarget, commit_sha: str, entries: Mapping[str, ArchiveEntry]
    ) -> bool:
        async with self._uow() as uow:
            saved = await uow.manifest.save(target, commit_sha, entries)
            if saved:
                await uow.commit()
            return saved
