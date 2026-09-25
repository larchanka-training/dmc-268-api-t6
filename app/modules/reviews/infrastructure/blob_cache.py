"""Small process-local adapter for immutable file blobs.

Production cache backends can implement the same application port.  This
adapter deliberately never fetches from a provider: callers receive a miss or
an expiry signal and must not fall back to a mutable path lookup.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.modules.reviews.application.get_run_file_lines import (
    BLOB_CACHE_TTL,
    BlobCacheEntry,
    BlobCacheKey,
    BlobCacheStatus,
)
from app.modules.reviews.infrastructure.models import CachedFileBlob

SEVEN_DAYS = BLOB_CACHE_TTL


@dataclass(frozen=True)
class _CachedBlob:
    content: str
    expires_at: datetime


class InMemoryBlobCache:
    def __init__(self, now: Callable[[], datetime] | None = None) -> None:
        self._now = now or (lambda: datetime.now(UTC))
        self._entries: dict[BlobCacheKey, _CachedBlob] = {}

    async def put(self, key: BlobCacheKey, content: str, *, ttl: timedelta = SEVEN_DAYS) -> None:
        self._entries[key] = _CachedBlob(content=content, expires_at=self._now() + ttl)

    async def get(self, key: BlobCacheKey) -> BlobCacheEntry:
        entry = self._entries.get(key)
        if entry is None:
            return BlobCacheEntry(status=BlobCacheStatus.MISS, content=None)
        if entry.expires_at <= self._now():
            return BlobCacheEntry(status=BlobCacheStatus.EXPIRED, content=None)
        return BlobCacheEntry(status=BlobCacheStatus.HIT, content=entry.content)


class SqlAlchemyBlobCache:
    """Shared PostgreSQL cache usable by independently deployed processes."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._now = now or (lambda: datetime.now(UTC))

    async def put(self, key: BlobCacheKey, content: str, *, ttl: timedelta = SEVEN_DAYS) -> None:
        expires_at = self._now() + ttl
        statement = insert(CachedFileBlob).values(
            code_change_id=key.code_change_id,
            head_sha=key.head_sha,
            path=key.path,
            content=content,
            expires_at=expires_at,
        )
        statement = statement.on_conflict_do_update(
            index_elements=[
                CachedFileBlob.code_change_id,
                CachedFileBlob.head_sha,
                CachedFileBlob.path,
            ],
            set_={"content": content, "expires_at": expires_at},
        )
        async with self._session_factory.begin() as session:
            await session.execute(statement)

    async def get(self, key: BlobCacheKey) -> BlobCacheEntry:
        statement = select(CachedFileBlob.content, CachedFileBlob.expires_at).where(
            CachedFileBlob.code_change_id == key.code_change_id,
            CachedFileBlob.head_sha == key.head_sha,
            CachedFileBlob.path == key.path,
        )
        async with self._session_factory() as session:
            row = (await session.execute(statement)).one_or_none()
        if row is None:
            return BlobCacheEntry(status=BlobCacheStatus.MISS, content=None)
        content, expires_at = row
        if expires_at <= self._now():
            return BlobCacheEntry(status=BlobCacheStatus.EXPIRED, content=None)
        return BlobCacheEntry(status=BlobCacheStatus.HIT, content=content)
