"""Graph RAG candidates of the review context (#144, ADR 0001 §8-§9).

A candidate is read at the PR revision through the shared blob cache (M10). Every
distinct blob counts toward the read limit of a run whether the cache has it or not, so
what a run may show does not depend on the cache. The cache is read in its own short
session, a miss is read from the provider outside any transaction, and the cache write
is a unit of work of its own: no DB transaction spans a GitHub read.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Protocol
from uuid import UUID

from app.common.application.unit_of_work import UnitOfWork
from app.modules.reviews.application.ast_resolution import BINARY_SOURCE, FileUnavailable
from app.modules.reviews.application.ast_symbols import MAX_SOURCE_BYTES, AstFailureReason
from app.modules.reviews.application.get_run_file_lines import (
    BLOB_CACHE_TTL,
    BlobCache,
    BlobCacheKey,
    BlobCacheStatus,
    BlobCacheWriter,
)
from app.modules.reviews.application.policy_errors import PolicyError
from app.modules.reviews.application.run_failures import RunCancelled, RunFailure
from app.modules.reviews.application.vcs_diff import is_binary_content

_LOGGER = logging.getLogger(__name__)

MAX_RAG_READS = 20
RAG_LIMIT = "rag_limit"


class BlobSource(Protocol):
    """Blobs of the PR revision; a read failure raises."""

    async def read_blob(self, blob_sha: str) -> bytes: ...


class BlobCacheUnitOfWork(UnitOfWork, Protocol):
    @property
    def blob_cache(self) -> BlobCacheWriter | None: ...


class CachedBlobSource:
    """Candidate texts of one run's PR revision, read through the blob cache.

    A text gets the same size, binary and UTF-8 checks as an L4 read, on a hit as on a
    miss. A failed read, an unusable blob and a broken cache cost a read like any other
    and never fail the run. Reads are awaited one at a time, so the blobs admitted under
    the limit follow the order of the calls.
    """

    def __init__(
        self,
        *,
        repository_id: UUID,
        source: BlobSource,
        cache: BlobCache,
        uow_factory: Callable[[], BlobCacheUnitOfWork],
    ) -> None:
        self._repository_id = repository_id
        self._source = source
        self._cache = cache
        self._uow_factory = uow_factory
        self._texts: dict[str, str | FileUnavailable] = {}

    async def text(self, blob_sha: str) -> str | FileUnavailable:
        if blob_sha in self._texts:
            return self._texts[blob_sha]
        if len(self._texts) >= MAX_RAG_READS:
            return FileUnavailable(RAG_LIMIT)
        result = await self._read(blob_sha)
        self._texts[blob_sha] = result
        return result

    async def _read(self, blob_sha: str) -> str | FileUnavailable:
        key = BlobCacheKey(self._repository_id, blob_sha)
        try:
            entry = await self._cache.get(key)
        except Exception:
            # A broken cache is a miss: the blob is still read from the provider.
            _LOGGER.warning("RAG blob cache get failed blob_sha=%s", blob_sha, exc_info=True)
        else:
            if entry.status is BlobCacheStatus.HIT and entry.content is not None:
                # Other writers of the cache check less: a hit gets the checks of a read.
                return _checked(entry.content.encode("utf-8"))
        try:
            raw = await self._source.read_blob(blob_sha)
        except Exception as exc:
            if isinstance(exc, (RunCancelled, PolicyError)) or (
                isinstance(exc, RunFailure) and exc.error_code == "deadline_exceeded"
            ):
                raise
            # Extra context is best effort: the diff itself stays available.
            _LOGGER.warning("RAG blob read failed blob_sha=%s", blob_sha, exc_info=True)
            return FileUnavailable("source_read_failed")
        result = _checked(raw)
        if isinstance(result, str):
            await self._put(key, result)
        return result

    async def _put(self, key: BlobCacheKey, content: str) -> None:
        try:
            async with self._uow_factory() as uow:
                if uow.blob_cache is not None:
                    await uow.blob_cache.put(key, content, ttl=BLOB_CACHE_TTL)
                await uow.commit()
        except Exception:
            # The cache only saves a later read; the text is already in hand.
            _LOGGER.warning("RAG blob cache put failed blob_sha=%s", key.blob_sha, exc_info=True)


def _checked(raw: bytes) -> str | FileUnavailable:
    """The checks of an L4 read (``_Files.text`` in ``review_context``), in its order."""
    if len(raw) > MAX_SOURCE_BYTES:
        return FileUnavailable(AstFailureReason.SOURCE_TOO_LARGE.value)
    if is_binary_content(raw):
        return FileUnavailable(BINARY_SOURCE)
    try:
        return raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        return FileUnavailable(AstFailureReason.SOURCE_DECODE_FAILED.value)
