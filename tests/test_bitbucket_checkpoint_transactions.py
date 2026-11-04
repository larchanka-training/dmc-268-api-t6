"""Checkpoint writes commit only at the application transaction boundary."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Self
from uuid import UUID

import pytest

from app.modules.code_index.application.bitbucket_persistence import (
    BitbucketIndexTarget,
    PersistBitbucketPublishedManifest,
    PersistBitbucketTreeCheckpoint,
    SourceListingPage,
)
from app.modules.code_index.application.vcs import ArchiveEntry
from app.modules.integrations.bitbucket.application.request_credentials import BoundBitbucketTarget

_TARGET = BoundBitbucketTarget(UUID(int=1), UUID(int=2), UUID(int=3), UUID(int=4), 1, 1)
_PAGE = SourceListingPage("a" * 40, "", {"pagelen": "100"}, {"values": []}, 12)


@dataclass
class _TreeStore:
    calls: list[str]

    async def charge(
        self, target: BitbucketIndexTarget, sha: str, directory: str, params: dict[str, str]
    ) -> None:
        self.calls.append("charge")

    async def save(self, target: BitbucketIndexTarget, page: SourceListingPage) -> None:
        self.calls.append("save")

    async def add_bytes(
        self,
        target: BitbucketIndexTarget,
        sha: str,
        directory: str,
        params: dict[str, str],
        count: int,
    ) -> None:
        self.calls.append("bytes")

    async def load(self, target: BitbucketIndexTarget, sha: str) -> tuple[SourceListingPage, ...]:
        self.calls.append("load")
        return (_PAGE,)


@dataclass
class _ManifestStore:
    calls: list[str]

    async def save(
        self, target: BitbucketIndexTarget, sha: str, entries: Mapping[str, ArchiveEntry]
    ) -> bool:
        self.calls.append("manifest")
        return True

    async def load(self, target: BitbucketIndexTarget, sha: str) -> dict[str, ArchiveEntry] | None:
        return None


@dataclass
class _Uow:
    calls: list[str] = field(default_factory=list)

    @property
    def pages(self) -> _TreeStore:
        return _TreeStore(self.calls)

    @property
    def manifest(self) -> _ManifestStore:
        return _ManifestStore(self.calls)

    async def __aenter__(self) -> Self:
        self.calls.append("enter")
        return self

    async def __aexit__(self, *_: object) -> None:
        self.calls.append("exit")

    async def commit(self) -> None:
        self.calls.append("commit")


def test_application_commits_checkpoint_and_manifest_writes_after_store_returns() -> None:
    async def scenario() -> None:
        uow = _Uow()
        pages = PersistBitbucketTreeCheckpoint(lambda: uow)
        await pages.charge(_TARGET, _PAGE.target_sha, "", _PAGE.params)
        assert uow.calls == ["enter", "charge", "commit", "exit"]
        uow.calls.clear()
        await pages.add_bytes(_TARGET, _PAGE.target_sha, "", _PAGE.params, 12)
        assert uow.calls == ["enter", "bytes", "commit", "exit"]
        uow.calls.clear()
        await pages.save(_TARGET, _PAGE)
        assert uow.calls == ["enter", "save", "commit", "exit"]
        uow.calls.clear()
        manifest = PersistBitbucketPublishedManifest(lambda: uow)
        assert await manifest.save(_TARGET, _PAGE.target_sha, {"a.py": ArchiveEntry("a.py", 1)})
        assert uow.calls == ["enter", "manifest", "commit", "exit"]

    asyncio.run(scenario())


def test_application_does_not_commit_a_failed_checkpoint_write() -> None:
    class FailingPages(_TreeStore):
        async def save(self, target: BitbucketIndexTarget, page: SourceListingPage) -> None:
            self.calls.append("save")
            raise RuntimeError("store failed")

    class FailingUow(_Uow):
        @property
        def pages(self) -> _TreeStore:
            return FailingPages(self.calls)

    async def scenario() -> None:
        uow = FailingUow()
        with pytest.raises(RuntimeError, match="store failed"):
            await PersistBitbucketTreeCheckpoint(lambda: uow).save(_TARGET, _PAGE)
        assert uow.calls == ["enter", "save", "exit"]

    asyncio.run(scenario())
