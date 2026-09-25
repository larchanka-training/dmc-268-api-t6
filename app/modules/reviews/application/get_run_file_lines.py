"""Read immutable, cached file blobs for a run in bounded line pages."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from enum import StrEnum
from typing import Protocol
from uuid import UUID

BLOB_CACHE_TTL = timedelta(days=7)


@dataclass(frozen=True)
class BlobCacheKey:
    """Immutable revision-qualified file identity from the persisted diff snapshot."""

    code_change_id: UUID
    head_sha: str
    path: str


class BlobCacheStatus(StrEnum):
    HIT = "hit"
    MISS = "miss"
    EXPIRED = "expired"


@dataclass(frozen=True)
class BlobCacheEntry:
    status: BlobCacheStatus
    content: str | None


@dataclass(frozen=True)
class FileLinesPage:
    path: str
    start_line: int
    lines: list[str]
    total_lines: int
    next_offset: int | None


class FileLinesNotFound(Exception):
    """The run/path is not an owned snapshot or its cached blob is unavailable."""


class FileLinesExpired(Exception):
    """The immutable blob was present but outlived its cache retention window."""


class RunFileRepository(Protocol):
    async def get_run_file_key(self, run_id: UUID, path: str) -> BlobCacheKey | None: ...


class BlobCache(Protocol):
    async def get(self, key: BlobCacheKey) -> BlobCacheEntry: ...


class BlobCacheWriter(BlobCache, Protocol):
    async def put(self, key: BlobCacheKey, content: str, *, ttl: timedelta) -> None: ...


class GetRunFileLines:
    def __init__(self, repository: RunFileRepository, cache: BlobCache) -> None:
        self._repository = repository
        self._cache = cache

    async def execute(self, run_id: UUID, path: str, offset: int, limit: int) -> FileLinesPage:
        _validate_path(path)
        key = await self._repository.get_run_file_key(run_id, path)
        if key is None:
            raise FileLinesNotFound

        entry = await self._cache.get(key)
        if entry.status is BlobCacheStatus.EXPIRED:
            raise FileLinesExpired
        if entry.status is not BlobCacheStatus.HIT or entry.content is None:
            raise FileLinesNotFound

        lines = entry.content.splitlines()
        if offset > len(lines):
            raise ValueError("offset exceeds file length")
        page_lines = lines[offset : offset + limit]
        next_offset = offset + len(page_lines)
        return FileLinesPage(
            path=path,
            start_line=offset + 1,
            lines=page_lines,
            total_lines=len(lines),
            next_offset=next_offset if next_offset < len(lines) else None,
        )


def _validate_path(path: str) -> None:
    invalid_part = any(part in {"", ".", ".."} for part in path.split("/"))
    if path.startswith("/") or "\\" in path or invalid_part:
        raise ValueError("path must be a relative repository path")
