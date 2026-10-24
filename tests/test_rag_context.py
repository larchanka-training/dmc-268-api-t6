"""Graph RAG context: candidate blobs are read through the blob cache (#144, M10)."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import timedelta
from types import TracebackType
from typing import Self
from uuid import UUID

import pytest

from app.modules.reviews.application.ast_resolution import FileUnavailable
from app.modules.reviews.application.get_run_file_lines import (
    BlobCacheEntry,
    BlobCacheKey,
    BlobCacheStatus,
)
from app.modules.reviews.application.rag_context import CachedBlobSource
from app.modules.reviews.application.run_failures import RunCancelled, RunFailure

_REPOSITORY = UUID(int=1)
_OTHER_REPOSITORY = UUID(int=2)


def _sha(number: int) -> str:
    return f"{number:040x}"


@dataclass
class BlobStore:
    """The shared cache rows, the PR revision blobs, and what touched them."""

    cached: dict[BlobCacheKey, tuple[BlobCacheStatus, str]] = field(default_factory=dict)
    blobs: dict[str, bytes] = field(default_factory=dict)
    failing_reads: dict[str, Exception] = field(default_factory=dict)
    fail_get: bool = False
    fail_put: bool = False
    gets: list[str] = field(default_factory=list)
    reads: list[str] = field(default_factory=list)
    puts: list[tuple[BlobCacheKey, str, timedelta]] = field(default_factory=list)
    commits: int = 0
    open_units: int = 0
    # How many units of work were open at each provider read: the rule is zero.
    units_open_at_read: list[int] = field(default_factory=list)


@dataclass
class PrRevisionBlobs:
    store: BlobStore

    async def read_blob(self, blob_sha: str) -> bytes:
        self.store.units_open_at_read.append(self.store.open_units)
        self.store.reads.append(blob_sha)
        if blob_sha in self.store.failing_reads:
            raise self.store.failing_reads[blob_sha]
        return self.store.blobs[blob_sha]


@dataclass
class SharedBlobCache:
    store: BlobStore

    async def get(self, key: BlobCacheKey) -> BlobCacheEntry:
        self.store.gets.append(key.blob_sha)
        if self.store.fail_get:
            raise ConnectionError("cache database unavailable")
        if key not in self.store.cached:
            return BlobCacheEntry(status=BlobCacheStatus.MISS, content=None)
        status, content = self.store.cached[key]
        return BlobCacheEntry(
            status=status, content=content if status is BlobCacheStatus.HIT else None
        )

    async def put(self, key: BlobCacheKey, content: str, *, ttl: timedelta) -> None:
        if self.store.fail_put:
            raise ConnectionError("cache database unavailable")
        self.store.puts.append((key, content, ttl))


class BlobCacheUow:
    def __init__(self, store: BlobStore) -> None:
        self._store = store

    @property
    def blob_cache(self) -> SharedBlobCache:
        return SharedBlobCache(self._store)

    async def __aenter__(self) -> Self:
        self._store.open_units += 1
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self._store.open_units -= 1
        return None

    async def commit(self) -> None:
        self._store.commits += 1

    async def rollback(self) -> None:
        return None


def _source(store: BlobStore, repository_id: UUID = _REPOSITORY) -> CachedBlobSource:
    return CachedBlobSource(
        repository_id=repository_id,
        source=PrRevisionBlobs(store),
        cache=SharedBlobCache(store),
        uow_factory=lambda: BlobCacheUow(store),
    )


def test_a_hit_does_not_read_the_blob() -> None:
    store = BlobStore(
        cached={BlobCacheKey(_REPOSITORY, _sha(1)): (BlobCacheStatus.HIT, "def save():\n")}
    )

    text = asyncio.run(_source(store).text(_sha(1)))

    assert text == "def save():\n"
    assert store.gets == [_sha(1)]
    assert store.reads == []
    assert store.puts == []


def test_a_miss_reads_once_and_puts_in_a_unit_of_work_of_its_own() -> None:
    store = BlobStore(blobs={_sha(1): b"def save():\n"})
    source = _source(store)

    async def scenario() -> tuple[str | FileUnavailable, str | FileUnavailable]:
        return await source.text(_sha(1)), await source.text(_sha(1))

    first, again = asyncio.run(scenario())

    assert first == again == "def save():\n"
    assert store.gets == [_sha(1)]
    assert store.reads == [_sha(1)]
    # No transaction is open while the provider is read.
    assert store.units_open_at_read == [0]
    assert store.puts == [(BlobCacheKey(_REPOSITORY, _sha(1)), "def save():\n", timedelta(days=7))]
    assert store.commits == 1
    assert store.open_units == 0


def test_an_expired_entry_is_read_again_and_refreshed() -> None:
    store = BlobStore(
        cached={BlobCacheKey(_REPOSITORY, _sha(1)): (BlobCacheStatus.EXPIRED, "old\n")},
        blobs={_sha(1): b"def save():\n"},
    )

    text = asyncio.run(_source(store).text(_sha(1)))

    assert text == "def save():\n"
    assert store.reads == [_sha(1)]
    assert [key for key, _, _ in store.puts] == [BlobCacheKey(_REPOSITORY, _sha(1))]


def test_the_cache_is_keyed_by_the_run_repository() -> None:
    store = BlobStore(
        cached={BlobCacheKey(_OTHER_REPOSITORY, _sha(1)): (BlobCacheStatus.HIT, "other\n")},
        blobs={_sha(1): b"mine\n"},
    )

    text = asyncio.run(_source(store).text(_sha(1)))

    assert text == "mine\n"
    assert store.reads == [_sha(1)]
    assert [key for key, _, _ in store.puts] == [BlobCacheKey(_REPOSITORY, _sha(1))]


def test_hits_and_misses_both_count_and_the_21st_distinct_blob_is_rag_limit() -> None:
    hits = {
        BlobCacheKey(_REPOSITORY, _sha(number)): (BlobCacheStatus.HIT, f"hit {number}\n")
        for number in range(1, 11)
    }
    misses = {_sha(number): f"miss {number}\n".encode() for number in range(11, 22)}
    store = BlobStore(cached=hits, blobs=misses)
    source = _source(store)

    async def scenario() -> list[str | FileUnavailable]:
        texts = [await source.text(_sha(number)) for number in range(1, 22)]
        # A blob already read is not a new read.
        texts.append(await source.text(_sha(1)))
        return texts

    texts = asyncio.run(scenario())

    assert texts[:10] == [f"hit {number}\n" for number in range(1, 11)]
    assert texts[10:20] == [f"miss {number}\n" for number in range(11, 21)]
    assert texts[20] == FileUnavailable("rag_limit")
    assert texts[21] == "hit 1\n"
    # The blob over the limit is neither looked up nor read.
    assert len(store.gets) == 20
    assert _sha(21) not in store.gets
    assert store.reads == [_sha(number) for number in range(11, 21)]


def test_a_failed_read_counts_toward_the_limit() -> None:
    store = BlobStore(
        blobs={_sha(number): b"x = 1\n" for number in range(2, 22)},
        failing_reads={_sha(1): RuntimeError("GitHub 502")},
    )
    source = _source(store)

    async def scenario() -> list[str | FileUnavailable]:
        return [await source.text(_sha(number)) for number in range(1, 22)]

    texts = asyncio.run(scenario())

    assert texts[0] == FileUnavailable("source_read_failed")
    assert texts[1:20] == ["x = 1\n"] * 19
    assert texts[20] == FileUnavailable("rag_limit")
    assert [key.blob_sha for key, _, _ in store.puts] == [_sha(number) for number in range(2, 21)]


@pytest.mark.parametrize(
    ("raw", "reason"),
    [
        (b"x" * 262_145, "source_too_large"),
        (b"x = 1\n\x00\n", "binary"),
        (b"%PDF-1.7\n", "binary"),
        (b"x = '\xff'\n", "source_decode_failed"),
    ],
    ids=["too_large", "nul", "magic_number", "not_utf8"],
)
def test_a_read_blob_gets_the_size_binary_and_utf8_checks(raw: bytes, reason: str) -> None:
    store = BlobStore(blobs={_sha(1): raw})

    text = asyncio.run(_source(store).text(_sha(1)))

    assert text == FileUnavailable(reason)
    assert store.puts == []


@pytest.mark.parametrize(
    ("content", "reason"),
    [("x" * 262_145, "source_too_large"), ("%PDF-1.7\n", "binary")],
    ids=["too_large", "magic_number"],
)
def test_a_cached_text_gets_the_same_checks_as_a_read(content: str, reason: str) -> None:
    # Other writers of the cache accept larger files and no magic numbers; a hit must
    # not show what a miss would withhold.
    store = BlobStore(cached={BlobCacheKey(_REPOSITORY, _sha(1)): (BlobCacheStatus.HIT, content)})

    text = asyncio.run(_source(store).text(_sha(1)))

    assert text == FileUnavailable(reason)
    assert store.reads == []


def test_a_failing_put_is_logged_and_the_text_is_returned(
    caplog: pytest.LogCaptureFixture,
) -> None:
    store = BlobStore(blobs={_sha(1): b"def save():\n"}, fail_put=True)

    with caplog.at_level(logging.WARNING, logger="app.modules.reviews.application.rag_context"):
        text = asyncio.run(_source(store).text(_sha(1)))

    assert text == "def save():\n"
    assert store.commits == 0
    assert store.open_units == 0
    assert [record.getMessage() for record in caplog.records] == [
        f"RAG blob cache put failed blob_sha={_sha(1)}"
    ]


def test_a_failing_cache_read_is_logged_and_the_blob_is_read(
    caplog: pytest.LogCaptureFixture,
) -> None:
    store = BlobStore(blobs={_sha(1): b"def save():\n"}, fail_get=True)

    with caplog.at_level(logging.WARNING, logger="app.modules.reviews.application.rag_context"):
        text = asyncio.run(_source(store).text(_sha(1)))

    assert text == "def save():\n"
    assert store.reads == [_sha(1)]
    assert [record.getMessage() for record in caplog.records] == [
        f"RAG blob cache get failed blob_sha={_sha(1)}"
    ]


@pytest.mark.parametrize(
    "error", [RunCancelled(), RunFailure("deadline_exceeded", "the run deadline passed")]
)
def test_cancellation_and_the_deadline_are_not_a_read_failure(error: Exception) -> None:
    store = BlobStore(failing_reads={_sha(1): error})

    with pytest.raises(type(error)):
        asyncio.run(_source(store).text(_sha(1)))
