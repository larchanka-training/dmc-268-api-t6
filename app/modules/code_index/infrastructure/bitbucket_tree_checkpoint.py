"""Durable, bounded pages for Bitbucket pinned tree traversal."""

from __future__ import annotations

import json
from collections.abc import Mapping
from functools import partial
from hashlib import sha256

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.common.infrastructure.db.unit_of_work import SqlAlchemyUnitOfWork
from app.modules.code_index.application.bitbucket_persistence import (
    BitbucketIndexTarget,
    PersistBitbucketPublishedManifest,
    PersistBitbucketRawCheckpoint,
    PersistBitbucketTreeCheckpoint,
    PublishedManifestStore,
    RawBlobCheckpoint,
    SourceListingCheckpoint,
    SourceListingPage,
)
from app.modules.code_index.application.vcs import ArchiveEntry, IndexLimitExceeded, VcsUnavailable
from app.modules.code_index.domain.enums import IndexStatus
from app.modules.code_index.infrastructure.models import (
    BitbucketPublishedManifest,
    BitbucketRawCheckpointBlob,
    BitbucketTreeCheckpointPage,
    CodeIndexState,
)


def _key(value: str) -> str:
    return sha256(value.encode()).hexdigest()


def _cursor(params: dict[str, str]) -> str:
    return json.dumps(params, sort_keys=True, separators=(",", ":"))


class _TreeCheckpointRepository:
    """Session-bound page writes; the application use case commits."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def load(self, target: BitbucketIndexTarget, sha: str) -> tuple[SourceListingPage, ...]:
        session = self._session
        state = await session.get(CodeIndexState, target.repository_id, with_for_update=True)
        rows = (
            await session.scalars(
                select(BitbucketTreeCheckpointPage).where(
                    BitbucketTreeCheckpointPage.repository_id == target.repository_id
                )
            )
        ).all()
        if state is None or state.purge_reason is not None:
            if rows:
                await session.execute(
                    delete(BitbucketTreeCheckpointPage).where(
                        BitbucketTreeCheckpointPage.repository_id == target.repository_id
                    )
                )
            return ()
        if any(
            row.target_sha != sha
            or row.binding_generation != target.binding_generation
            or row.index_generation != state.generation
            for row in rows
        ):
            await session.execute(
                delete(BitbucketTreeCheckpointPage).where(
                    BitbucketTreeCheckpointPage.repository_id == target.repository_id
                )
            )
            return ()
        return tuple(
            SourceListingPage(
                row.target_sha,
                row.directory,
                row.cursor,
                row.payload,
                row.bytes_read,
                row.attempts,
                row.complete,
            )
            for row in rows
        )

    async def invalidate(self, target: BitbucketIndexTarget, sha: str) -> None:
        session = self._session
        state = await session.get(CodeIndexState, target.repository_id, with_for_update=True)
        if state is None:
            return
        await session.execute(
            delete(BitbucketTreeCheckpointPage).where(
                BitbucketTreeCheckpointPage.repository_id == target.repository_id,
                BitbucketTreeCheckpointPage.target_sha == sha,
                BitbucketTreeCheckpointPage.binding_generation == target.binding_generation,
                BitbucketTreeCheckpointPage.index_generation == state.generation,
            )
        )

    async def charge(
        self, target: BitbucketIndexTarget, sha: str, directory: str, params: dict[str, str]
    ) -> None:
        session = self._session
        state = await session.get(CodeIndexState, target.repository_id, with_for_update=True)
        if state is None or state.purge_reason is not None:
            raise VcsUnavailable("Bitbucket index checkpoint lost its repository")
        existing = await session.get(
            BitbucketTreeCheckpointPage,
            (target.repository_id, _key(directory), _key(_cursor(params))),
        )
        if existing is not None:
            if (
                existing.target_sha != sha
                or existing.binding_generation != target.binding_generation
                or existing.index_generation != state.generation
                or existing.directory != directory
                or existing.cursor != params
                or existing.complete
            ):
                raise VcsUnavailable("Bitbucket index checkpoint changed")
            existing.attempts += 1
            return
        session.add(
            BitbucketTreeCheckpointPage(
                repository_id=target.repository_id,
                directory_key=_key(directory),
                cursor_key=_key(_cursor(params)),
                target_sha=sha,
                binding_generation=target.binding_generation,
                index_generation=state.generation,
                directory=directory,
                cursor=params,
                payload={},
                bytes_read=0,
                attempts=1,
                complete=False,
            )
        )

    async def save(self, target: BitbucketIndexTarget, page: SourceListingPage) -> None:
        session = self._session
        state = await session.get(CodeIndexState, target.repository_id, with_for_update=True)
        if state is None or state.purge_reason is not None:
            raise VcsUnavailable("Bitbucket index checkpoint lost its repository")
        existing = await session.get(
            BitbucketTreeCheckpointPage,
            (target.repository_id, _key(page.directory), _key(_cursor(page.params))),
        )
        if existing is None:
            raise VcsUnavailable("Bitbucket index checkpoint has no page request")
        if (
            existing.target_sha != page.target_sha
            or existing.binding_generation != target.binding_generation
            or existing.index_generation != state.generation
            or existing.directory != page.directory
            or existing.cursor != page.params
        ):
            raise VcsUnavailable("Bitbucket index checkpoint changed")
        if existing.complete:
            if existing.payload != page.payload:
                raise VcsUnavailable("Bitbucket index checkpoint changed")
            return
        existing.payload = page.payload
        existing.complete = True

    async def add_bytes(
        self,
        target: BitbucketIndexTarget,
        sha: str,
        directory: str,
        params: dict[str, str],
        count: int,
    ) -> None:
        session = self._session
        state = await session.get(CodeIndexState, target.repository_id, with_for_update=True)
        if state is None or state.purge_reason is not None:
            raise VcsUnavailable("Bitbucket index checkpoint lost its repository")
        existing = await session.get(
            BitbucketTreeCheckpointPage,
            (target.repository_id, _key(directory), _key(_cursor(params))),
        )
        if (
            existing is None
            or existing.target_sha != sha
            or existing.binding_generation != target.binding_generation
            or existing.index_generation != state.generation
            or existing.directory != directory
            or existing.cursor != params
            or existing.complete
        ):
            raise VcsUnavailable("Bitbucket index checkpoint changed")
        existing.bytes_read += count


class _RawCheckpointRepository:
    """Session-bound raw writes, fenced to one target and capped at 100 MiB."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def load_raw(self, target: BitbucketIndexTarget, sha: str, path: str) -> bytes | None:
        session = self._session
        state = await session.get(CodeIndexState, target.repository_id)
        if state is None or state.purge_reason is not None:
            return None
        row = await session.get(BitbucketRawCheckpointBlob, (target.repository_id, _key(path)))
        if (
            row is None
            or row.target_sha != sha
            or row.binding_generation != target.binding_generation
            or row.index_generation != state.generation
            or row.path != path
        ):
            return None
        return bytes(row.body)

    async def save_raw(
        self, target: BitbucketIndexTarget, sha: str, path: str, body: bytes
    ) -> None:
        if len(body) > 100 * 1024 * 1024:
            raise IndexLimitExceeded("download_bytes")
        session = self._session
        state = await session.get(CodeIndexState, target.repository_id, with_for_update=True)
        if state is None or state.purge_reason is not None:
            raise VcsUnavailable("Bitbucket raw checkpoint lost its repository")
        first = await session.scalar(
            select(BitbucketRawCheckpointBlob)
            .where(BitbucketRawCheckpointBlob.repository_id == target.repository_id)
            .limit(1)
        )
        if first is not None and (
            first.target_sha != sha
            or first.binding_generation != target.binding_generation
            or first.index_generation != state.generation
        ):
            await session.execute(
                delete(BitbucketRawCheckpointBlob).where(
                    BitbucketRawCheckpointBlob.repository_id == target.repository_id
                )
            )
            state.raw_checkpoint_bytes = 0
            first = None
        if first is None and state.raw_checkpoint_bytes:
            state.raw_checkpoint_bytes = 0
        existing = await session.get(BitbucketRawCheckpointBlob, (target.repository_id, _key(path)))
        if existing is not None:
            if (
                existing.target_sha != sha
                or existing.binding_generation != target.binding_generation
                or existing.index_generation != state.generation
                or existing.path != path
                or bytes(existing.body) != body
            ):
                raise VcsUnavailable("Bitbucket raw checkpoint changed")
            return
        if state.raw_checkpoint_bytes + len(body) > 100 * 1024 * 1024:
            raise IndexLimitExceeded("download_bytes")
        session.add(
            BitbucketRawCheckpointBlob(
                repository_id=target.repository_id,
                path_key=_key(path),
                target_sha=sha,
                binding_generation=target.binding_generation,
                index_generation=state.generation,
                path=path,
                body=body,
            )
        )
        state.raw_checkpoint_bytes += len(body)

    async def clear(self, target: BitbucketIndexTarget, sha: str) -> None:
        session = self._session
        state = await session.get(CodeIndexState, target.repository_id, with_for_update=True)
        if state is None:
            return
        first = await session.scalar(
            select(BitbucketRawCheckpointBlob)
            .where(BitbucketRawCheckpointBlob.repository_id == target.repository_id)
            .limit(1)
        )
        if first is not None and (
            first.target_sha == sha
            and first.binding_generation == target.binding_generation
            and first.index_generation == state.generation
        ):
            await session.execute(
                delete(BitbucketRawCheckpointBlob).where(
                    BitbucketRawCheckpointBlob.repository_id == target.repository_id
                )
            )
            state.raw_checkpoint_bytes = 0
        await session.execute(
            delete(BitbucketTreeCheckpointPage).where(
                BitbucketTreeCheckpointPage.repository_id == target.repository_id,
                BitbucketTreeCheckpointPage.target_sha == sha,
                BitbucketTreeCheckpointPage.binding_generation == target.binding_generation,
                BitbucketTreeCheckpointPage.index_generation == state.generation,
            )
        )


class _PublishedManifestRepository:
    """Session-bound published manifest writes; the application use case commits."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def load(
        self, target: BitbucketIndexTarget, base_sha: str
    ) -> dict[str, ArchiveEntry] | None:
        session = self._session
        state = await session.get(CodeIndexState, target.repository_id, with_for_update=True)
        row = await session.get(BitbucketPublishedManifest, target.repository_id)
        if (
            state is None
            or state.purge_reason is not None
            or state.indexed_commit_sha != base_sha
            or state.published_publication == 0
            or row is None
            or row.commit_sha != base_sha
            or row.binding_generation != target.binding_generation
            or row.index_generation != state.generation
            or not isinstance(row.entries, list)
            or len(row.entries) > 100_000
        ):
            return None
        result: dict[str, ArchiveEntry] = {}
        for item in row.entries:
            if not isinstance(item, dict):
                return None
            path, size, symlink = item.get("path"), item.get("size"), item.get("symlink")
            if (
                not isinstance(path, str)
                or type(size) is not int
                or type(symlink) is not bool
                or path in result
            ):
                return None
            result[path] = ArchiveEntry(path, size, symlink=symlink)
        return result

    async def save(
        self,
        target: BitbucketIndexTarget,
        commit_sha: str,
        entries: Mapping[str, ArchiveEntry],
    ) -> bool:
        if len(entries) > 100_000:
            return False
        session = self._session
        state = await session.get(CodeIndexState, target.repository_id, with_for_update=True)
        if (
            state is None
            or state.purge_reason is not None
            or state.status is not IndexStatus.READY
            or state.indexed_commit_sha != commit_sha
        ):
            return False
        payload: list[dict[str, object]] = [
            {"path": path, "size": entry.size, "symlink": entry.symlink}
            for path, entry in sorted(entries.items())
        ]
        row = await session.get(BitbucketPublishedManifest, target.repository_id)
        if row is None:
            session.add(
                BitbucketPublishedManifest(
                    repository_id=target.repository_id,
                    commit_sha=commit_sha,
                    binding_generation=target.binding_generation,
                    index_generation=state.generation,
                    entries=payload,
                )
            )
        else:
            row.commit_sha = commit_sha
            row.binding_generation = target.binding_generation
            row.index_generation = state.generation
            row.entries = payload
        return True


class SqlAlchemyBitbucketPersistenceUnitOfWork(SqlAlchemyUnitOfWork):
    @property
    def pages(self) -> SourceListingCheckpoint:
        return _TreeCheckpointRepository(self.session)

    @property
    def raw(self) -> RawBlobCheckpoint:
        return _RawCheckpointRepository(self.session)

    @property
    def manifest(self) -> PublishedManifestStore:
        return _PublishedManifestRepository(self.session)


class SqlAlchemyBitbucketTreeCheckpoint(PersistBitbucketTreeCheckpoint):
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        super().__init__(partial(SqlAlchemyBitbucketPersistenceUnitOfWork, sessions))


class SqlAlchemyBitbucketRawCheckpoint(PersistBitbucketRawCheckpoint):
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        super().__init__(partial(SqlAlchemyBitbucketPersistenceUnitOfWork, sessions))


class SqlAlchemyBitbucketPublishedManifest(PersistBitbucketPublishedManifest):
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        super().__init__(partial(SqlAlchemyBitbucketPersistenceUnitOfWork, sessions))
