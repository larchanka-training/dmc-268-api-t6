"""Bitbucket index snapshots use immutable UUIDs and bounded fixed-origin reads."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Mapping
from datetime import timedelta

import httpx
import pytest

from app.modules.code_index.application.bitbucket_persistence import (
    BitbucketIndexTarget,
    SourceListingPage,
)
from app.modules.code_index.application.vcs import (
    Ancestry,
    ArchiveEntry,
    ChangedPath,
    ChangeStatus,
    Comparison,
    VcsUnavailable,
)
from app.modules.code_index.infrastructure.bitbucket_snapshot_source import (
    BitbucketSourceBudgets,
)
from tests.bitbucket_snapshot_fakes import (
    _PREFIX,
    _SHA,
    _TARGET,
    _Credentials,
    _source,
)


class _Pages:
    def __init__(self) -> None:
        self.saved: list[SourceListingPage] = []
        self.invalidations = 0

    async def load(self, target: BitbucketIndexTarget, sha: str) -> tuple[SourceListingPage, ...]:
        assert target == _TARGET
        return tuple(page for page in self.saved if page.target_sha == sha)

    async def save(self, target: BitbucketIndexTarget, page: SourceListingPage) -> None:
        assert target == _TARGET
        self.saved.append(page)

    async def charge(
        self, target: BitbucketIndexTarget, sha: str, directory: str, params: dict[str, str]
    ) -> None:
        assert target == _TARGET

    async def add_bytes(
        self,
        target: BitbucketIndexTarget,
        sha: str,
        directory: str,
        params: dict[str, str],
        count: int,
    ) -> None:
        assert target == _TARGET

    async def invalidate(self, target: BitbucketIndexTarget, sha: str) -> None:
        assert target == _TARGET and sha == _SHA
        self.invalidations += 1
        self.saved.clear()


@pytest.mark.parametrize("final_size", [2, 3])
def test_completed_page_survives_restart_and_429_does_not_publish_archive(
    final_size: int,
) -> None:
    pages = _Pages()
    seen: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if request.url.path.endswith(f"/src/{_SHA}/"):
            if request.url.params.get("page") == "2":
                if seen.count(str(request.url)) == 1:
                    return httpx.Response(429, headers={"Retry-After": "1"})
                return httpx.Response(200, json={"size": final_size, "values": []})
            return httpx.Response(
                200,
                json={
                    "size": 2,
                    "values": [
                        {
                            "path": "a.py",
                            "type": "commit_file",
                            "size": 1,
                            "commit": {"hash": _SHA},
                        },
                        {
                            "path": "b.py",
                            "type": "commit_file",
                            "size": 1,
                            "commit": {"hash": _SHA},
                        },
                    ],
                    "next": f"https://api.bitbucket.org{_PREFIX}/src/{_SHA}/?page=2",
                },
            )
        if request.url.path.endswith("/a.py") or request.url.path.endswith("/b.py"):
            return httpx.Response(200, content=b"x")
        raise AssertionError(str(request.url))

    async def exercise() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            first = _source(_Credentials(), client, checkpoint=pages)
            with pytest.raises(VcsUnavailable) as error:
                await first.archive(_SHA, max_bytes=100_000)
            assert error.value.rate_limited
            assert len(pages.saved) == 1
            second = _source(_Credentials(), client, checkpoint=pages)
            if final_size == 2:
                archive = await second.archive(_SHA, max_bytes=100_000)
                assert archive.commit_sha == _SHA
            else:
                with pytest.raises(VcsUnavailable, match="changed a source listing"):
                    await second.archive(_SHA, max_bytes=100_000)
                assert len(pages.saved) == 1
            assert second.listing_request_count == 1
            assert second.raw_read_count == (2 if final_size == 2 else 0)

    asyncio.run(exercise())
    assert sum(url.endswith(f"/src/{_SHA}/?pagelen=100") for url in seen) == 1


@pytest.mark.parametrize(("expired_status", "restart_status"), [(400, 200), (404, 200), (400, 400)])
def test_expired_restored_cursor_resets_checkpoint_and_restarts_listing_once(
    expired_status: int,
    restart_status: int,
) -> None:
    pages = _Pages()
    root_requests = 0
    next_requests = 0

    def handle(request: httpx.Request) -> httpx.Response:
        nonlocal root_requests, next_requests
        if request.url.params.get("page") == "2":
            next_requests += 1
            if next_requests == 1:
                return httpx.Response(429, headers={"Retry-After": "60"})
            if next_requests == 2:
                return httpx.Response(expired_status)
            if restart_status != 200:
                return httpx.Response(restart_status)
            return httpx.Response(200, json={"size": 1, "values": []})
        root_requests += 1
        return httpx.Response(
            200,
            json={
                "size": 1,
                "values": [
                    {
                        "path": "a.py",
                        "type": "commit_file",
                        "size": 1,
                        "commit": {"hash": _SHA},
                    }
                ],
                "next": f"https://api.bitbucket.org{_PREFIX}/src/{_SHA}/?page=2",
            },
        )

    async def exercise() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            with pytest.raises(VcsUnavailable) as caught:
                await _source(_Credentials(), client, checkpoint=pages).manifest(_SHA)
            assert caught.value.rate_limited
            second = _source(_Credentials(), client, checkpoint=pages)
            if restart_status == 200:
                assert set(await second.manifest(_SHA)) == {"a.py"}
            else:
                with pytest.raises(VcsUnavailable) as final:
                    await second.manifest(_SHA)
                assert final.value.http_status == 400

    asyncio.run(exercise())
    assert (root_requests, next_requests, pages.invalidations) == (2, 3, 1)


def test_interrupted_listing_records_bytes_without_blocking_a_complete_retry() -> None:
    partial = b" " * 25
    complete = b'{"values":[],"size":0}'

    class Interrupted(httpx.AsyncByteStream):
        async def __aiter__(self) -> AsyncIterator[bytes]:
            yield partial
            raise httpx.ReadError("stream interrupted")

    class Pages:
        def __init__(self) -> None:
            self.attempts = 0
            self.bytes_read = 0
            self.saved: SourceListingPage | None = None

        async def load(
            self, target: BitbucketIndexTarget, sha: str
        ) -> tuple[SourceListingPage, ...]:
            assert target == _TARGET
            if not self.attempts:
                return ()
            return (
                SourceListingPage(
                    sha, "", {"pagelen": "100"}, {}, self.bytes_read, self.attempts, False
                ),
            )

        async def charge(
            self, target: BitbucketIndexTarget, sha: str, directory: str, params: dict[str, str]
        ) -> None:
            self.attempts += 1

        async def add_bytes(
            self,
            target: BitbucketIndexTarget,
            sha: str,
            directory: str,
            params: dict[str, str],
            count: int,
        ) -> None:
            self.bytes_read += count

        async def save(self, target: BitbucketIndexTarget, page: SourceListingPage) -> None:
            self.saved = page

        async def invalidate(self, target: BitbucketIndexTarget, sha: str) -> None:
            self.saved = None

    pages = Pages()
    requests = 0

    def handle(_: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        if requests == 1:
            return httpx.Response(200, stream=Interrupted())
        return httpx.Response(200, content=complete)

    async def exercise() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            budgets = BitbucketSourceBudgets(read_bytes=len(partial) + len(complete) - 1)
            first = _source(_Credentials(), client, budgets=budgets, checkpoint=pages)
            with pytest.raises(VcsUnavailable, match="source request failed"):
                await first.archive(_SHA, max_bytes=100_000)
            assert pages.bytes_read == len(partial)
            second = _source(_Credentials(), client, budgets=budgets, checkpoint=pages)
            archive = await second.archive(_SHA, max_bytes=100_000)
            assert archive.commit_sha == _SHA

    asyncio.run(exercise())
    assert pages.bytes_read == len(partial) + len(complete)
    assert pages.saved is not None and pages.saved.complete
    assert requests == 2


def test_repeated_429_does_not_consume_completed_listing_budget_on_restart() -> None:
    class Pages:
        def __init__(self) -> None:
            self.attempts = 0
            self.saved: SourceListingPage | None = None

        async def load(
            self, target: BitbucketIndexTarget, sha: str
        ) -> tuple[SourceListingPage, ...]:
            assert target == _TARGET
            if not self.attempts:
                return ()
            return (SourceListingPage(sha, "", {"pagelen": "100"}, {}, 0, self.attempts, False),)

        async def charge(
            self, target: BitbucketIndexTarget, sha: str, directory: str, params: dict[str, str]
        ) -> None:
            self.attempts += 1

        async def add_bytes(
            self,
            target: BitbucketIndexTarget,
            sha: str,
            directory: str,
            params: dict[str, str],
            count: int,
        ) -> None:
            assert count == 22

        async def save(self, target: BitbucketIndexTarget, page: SourceListingPage) -> None:
            self.saved = page

        async def invalidate(self, target: BitbucketIndexTarget, sha: str) -> None:
            self.saved = None

    pages = Pages()
    requests = 0

    def handle(_: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        if requests <= 3:
            return httpx.Response(429, headers={"Retry-After": "60"})
        return httpx.Response(200, json={"size": 0, "values": []})

    async def exercise() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            for _ in range(3):
                source = _source(
                    _Credentials(),
                    client,
                    budgets=BitbucketSourceBudgets(listing_requests=3),
                    checkpoint=pages,
                )
                with pytest.raises(VcsUnavailable) as caught:
                    await source.manifest(_SHA)
                assert caught.value.rate_limited
            source = _source(
                _Credentials(),
                client,
                budgets=BitbucketSourceBudgets(listing_requests=3),
                checkpoint=pages,
            )
            assert await source.manifest(_SHA) == {}
            assert source.listing_request_count == 1

    asyncio.run(exercise())
    assert requests == 4
    assert pages.saved is not None and pages.saved.complete


def test_raw_reads_resume_after_rate_limit_without_refetching_completed_blobs() -> None:
    class Published:
        async def load(
            self, target: BitbucketIndexTarget, base_sha: str
        ) -> dict[str, ArchiveEntry] | None:
            raise AssertionError("incremental manifest is not used")

        async def save(
            self,
            target: BitbucketIndexTarget,
            commit_sha: str,
            entries: Mapping[str, ArchiveEntry],
        ) -> bool:
            assert target == _TARGET and commit_sha == _SHA
            assert len(entries) == 6
            return True

    class Checkpoint(_Pages):
        def __init__(self) -> None:
            super().__init__()
            self.raw: dict[str, bytes] = {}

        async def load_raw(self, target: BitbucketIndexTarget, sha: str, path: str) -> bytes | None:
            assert target == _TARGET and sha == _SHA
            return self.raw.get(path)

        async def save_raw(
            self, target: BitbucketIndexTarget, sha: str, path: str, body: bytes
        ) -> None:
            assert target == _TARGET and sha == _SHA
            self.raw[path] = body

        async def clear(self, target: BitbucketIndexTarget, sha: str) -> None:
            self.raw.clear()

    checkpoint = Checkpoint()
    raw_paths: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith(f"/src/{_SHA}/"):
            return httpx.Response(
                200,
                json={
                    "size": 6,
                    "values": [
                        {
                            "path": f"f{i}.py",
                            "type": "commit_file",
                            "size": 1,
                            "commit": {"hash": _SHA},
                        }
                        for i in range(6)
                    ],
                },
            )
        name = path.rsplit("/", 1)[-1]
        raw_paths.append(name)
        if name == "f3.py" and raw_paths.count("f3.py") == 1:
            return httpx.Response(429, headers={"Retry-After": "3600"})
        return httpx.Response(200, content=b"x")

    async def exercise() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            first = _source(
                _Credentials(), client, checkpoint=checkpoint, raw_checkpoint=checkpoint
            )
            with pytest.raises(VcsUnavailable) as caught:
                await first.archive(_SHA, max_bytes=100_000)
            assert caught.value.rate_limited
            assert caught.value.retry_after == timedelta(hours=1)
            assert set(checkpoint.raw) == {"f0.py", "f1.py", "f2.py"}
            second = _source(
                _Credentials(),
                client,
                checkpoint=checkpoint,
                raw_checkpoint=checkpoint,
                published_manifest=Published(),
            )
            archive = await second.archive(_SHA, max_bytes=100_000)
            assert archive.commit_sha == _SHA
            assert second.listing_request_count == 0
            assert second.raw_read_count == 3
            await second.publish_manifest(_SHA)
            assert checkpoint.raw == {}

    asyncio.run(exercise())
    assert raw_paths == ["f0.py", "f1.py", "f2.py", "f3.py", "f3.py", "f4.py", "f5.py"]


@pytest.mark.parametrize("saved_wrong_size", [False, True])
def test_wrong_size_raw_response_or_checkpoint_does_not_poison_next_worker(
    saved_wrong_size: bool,
) -> None:
    class Raw:
        def __init__(self) -> None:
            self.saved: dict[str, bytes] = {}
            self.clears = 0

        async def load_raw(self, target: BitbucketIndexTarget, sha: str, path: str) -> bytes | None:
            return self.saved.get(path)

        async def save_raw(
            self, target: BitbucketIndexTarget, sha: str, path: str, body: bytes
        ) -> None:
            if path in self.saved and self.saved[path] != body:
                raise VcsUnavailable("Bitbucket raw checkpoint changed")
            self.saved[path] = body

        async def clear(self, target: BitbucketIndexTarget, sha: str) -> None:
            self.clears += 1
            self.saved.clear()

    raw = Raw()
    if saved_wrong_size:
        raw.saved["a.py"] = b"xx"
    reads = 0

    def handle(request: httpx.Request) -> httpx.Response:
        nonlocal reads
        if request.url.path.endswith(f"/src/{_SHA}/"):
            return httpx.Response(
                200,
                json={
                    "size": 1,
                    "values": [
                        {
                            "path": "a.py",
                            "type": "commit_file",
                            "size": 1,
                            "commit": {"hash": _SHA},
                        }
                    ],
                },
            )
        assert request.url.path.endswith(f"/src/{_SHA}/a.py")
        reads += 1
        return httpx.Response(200, content=b"xx" if reads == 1 and not saved_wrong_size else b"x")

    async def exercise() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            if not saved_wrong_size:
                with pytest.raises(VcsUnavailable, match="size differs"):
                    await _source(_Credentials(), client, raw_checkpoint=raw).archive(
                        _SHA, max_bytes=100_000
                    )
                assert raw.saved == {}
            archive = await _source(_Credentials(), client, raw_checkpoint=raw).archive(
                _SHA, max_bytes=100_000
            )
            assert archive.commit_sha == _SHA
            if saved_wrong_size:
                reused = await _source(_Credentials(), client, raw_checkpoint=raw).archive(
                    _SHA, max_bytes=100_000
                )
                assert reused.commit_sha == _SHA

    asyncio.run(exercise())
    assert reads == (1 if saved_wrong_size else 2)
    assert raw.clears == int(saved_wrong_size)
    assert raw.saved == {"a.py": b"x"}


def test_metadata_429_never_publishes_partial_incremental_manifest() -> None:
    base = "b" * 40
    metadata_reads: list[str] = []
    published: list[set[str]] = []

    class Published:
        async def load(
            self, target: BitbucketIndexTarget, base_sha: str
        ) -> dict[str, ArchiveEntry] | None:
            assert target == _TARGET and base_sha == base
            return {}

        async def save(
            self,
            target: BitbucketIndexTarget,
            commit_sha: str,
            entries: Mapping[str, ArchiveEntry],
        ) -> bool:
            assert target == _TARGET and commit_sha == _SHA
            published.append(set(entries))
            return True

    def handle(request: httpx.Request) -> httpx.Response:
        assert request.url.params.get("format") == "meta"
        name = request.url.path.rsplit("/", 1)[-1]
        metadata_reads.append(name)
        if name == "b.py" and metadata_reads.count("b.py") == 1:
            return httpx.Response(429, headers={"Retry-After": "60"})
        return httpx.Response(
            200,
            json={
                "path": name,
                "commit": {"hash": _SHA},
                "type": "commit_file",
                "size": 1,
                "attributes": [],
            },
        )

    async def exercise() -> None:
        comparison = Comparison(
            Ancestry.AHEAD,
            (
                ChangedPath("a.py", ChangeStatus.ADDED),
                ChangedPath("b.py", ChangeStatus.ADDED),
            ),
            True,
        )
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            first = _source(_Credentials(), client, published_manifest=Published())
            with pytest.raises(VcsUnavailable) as caught:
                await first.prepare_incremental_manifest(base, _SHA, comparison)
            assert caught.value.rate_limited
            await first.publish_manifest(_SHA)
            assert published == []
            second = _source(_Credentials(), client, published_manifest=Published())
            assert await second.prepare_incremental_manifest(base, _SHA, comparison)
            await second.publish_manifest(_SHA)

    asyncio.run(exercise())
    assert metadata_reads == ["a.py", "b.py", "a.py", "b.py"]
    assert published == [{"a.py", "b.py"}]
