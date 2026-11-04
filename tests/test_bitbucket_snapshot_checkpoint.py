"""Bitbucket index snapshots use immutable UUIDs and bounded fixed-origin reads."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import httpx
import pytest

from app.modules.code_index.application.bitbucket_persistence import (
    BitbucketIndexTarget,
    SourceListingPage,
)
from app.modules.code_index.application.vcs import (
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


def test_interrupted_listing_counts_received_bytes_after_restart() -> None:
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
            raise AssertionError("an incomplete listing cannot become an archive")

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
            with pytest.raises(VcsUnavailable, match="work budget exhausted"):
                await second.archive(_SHA, max_bytes=100_000)

    asyncio.run(exercise())
    assert requests == 2
