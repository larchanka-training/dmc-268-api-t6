"""Independent provider-boundary proofs for Bitbucket snapshot safety."""

from __future__ import annotations

import asyncio
import time
from uuid import UUID

import httpx
import pytest

from app.modules.code_index.application.vcs import (
    IndexLimitExceeded,
    VcsAccessDenied,
    VcsUnavailable,
)
from app.modules.code_index.domain.limits import IndexLimits
from app.modules.code_index.infrastructure.bitbucket_snapshot_source import (
    BitbucketRepositorySnapshotSource,
)
from app.modules.code_index.infrastructure.tar_archive import TarArchiveReader
from app.modules.integrations.bitbucket.application.request_credentials import (
    BitbucketRequestCredential,
    BoundBitbucketTarget,
)

SHA = "a" * 40
REPO = UUID("11111111-1111-4111-8111-111111111111")
TARGET = BoundBitbucketTarget(
    UUID("22222222-2222-4222-8222-222222222222"),
    UUID("33333333-3333-4333-8333-333333333333"),
    UUID("44444444-4444-4444-8444-444444444444"),
    REPO,
    2,
    3,
)


class Credentials:
    async def credential_for(self, target: BoundBitbucketTarget) -> BitbucketRequestCredential:
        assert target == TARGET
        return BitbucketRequestCredential(target, "secret-token")

    async def checkpoint(self, target: BoundBitbucketTarget) -> None:
        assert target == TARGET


def source(
    client: httpx.AsyncClient, limits: IndexLimits | None = None
) -> BitbucketRepositorySnapshotSource:
    async def target() -> BoundBitbucketTarget:
        return TARGET

    return BitbucketRepositorySnapshotSource(
        client=client, target_for_repository=target, credentials=Credentials(), limits=limits
    )


def listing(
    *entries: dict[str, object], size: int | None = None, next_page: str | None = None
) -> httpx.Response:
    data: dict[str, object] = {"values": list(entries)}
    if size is not None:
        data["size"] = size
    if next_page is not None:
        data["next"] = next_page
    return httpx.Response(200, json=data)


def file_entry(
    path: str,
    size: int = 1,
    *,
    kind: str = "commit_file",
    attributes: list[str] | None = None,
    pin: str = SHA,
) -> dict[str, object]:
    return {
        "path": path,
        "type": kind,
        "size": None if kind == "commit_directory" else size,
        "attributes": attributes or [],
        "commit": {"hash": pin},
    }


def test_head_rejects_mismatched_repository_uuid() -> None:
    calls = 0

    def handle(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            200,
            json={"uuid": "{99999999-9999-4999-8999-999999999999}", "mainbranch": {"name": "main"}},
        )

    async def run() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            with pytest.raises(VcsAccessDenied, match="UUID"):
                await source(client).head()

    asyncio.run(run())
    assert calls == 1


def test_archive_rejects_unpinned_entry_before_raw_read() -> None:
    calls = 0

    def handle(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return listing(file_entry("a.py", pin="b" * 40))

    async def run() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            with pytest.raises(VcsUnavailable, match="changed commit"):
                await source(client).archive(SHA, max_bytes=10000)

    asyncio.run(run())
    assert calls == 1


@pytest.mark.parametrize("declared,body", [(1, b"two"), (3, b"x")])
def test_archive_rejects_file_size_mismatch(declared: int, body: bytes) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith(f"/src/{SHA}/"):
            return listing(file_entry("a.py", declared))
        return httpx.Response(200, content=body)

    async def run() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            with pytest.raises(VcsUnavailable, match="size differs"):
                await source(client).archive(SHA, max_bytes=10000)

    asyncio.run(run())


def test_declared_listing_size_must_match_actual_entries() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        return listing(file_entry("a.py"), size=2)

    async def run() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            with pytest.raises(VcsUnavailable, match="incomplete"):
                await source(client).archive(SHA, max_bytes=10000)

    asyncio.run(run())


def test_repeated_source_page_stops_before_third_request() -> None:
    calls = 0

    def handle(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return listing(next_page=str(request.url))

    async def run() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            with pytest.raises(VcsUnavailable, match="repeated a source page"):
                await source(client).archive(SHA, max_bytes=10000)

    asyncio.run(run())
    assert calls == 1


def test_remaining_budget_stops_before_oversized_second_file() -> None:
    raw_paths: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith(f"/src/{SHA}/"):
            return listing(file_entry("a.py", 1), file_entry("b.py", 1))
        raw_paths.append(request.url.path)
        return httpx.Response(200, content=b"xx")

    async def run() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            with pytest.raises(IndexLimitExceeded, match="download_bytes"):
                await source(client).archive(SHA, max_bytes=1)

    asyncio.run(run())
    assert raw_paths == []


@pytest.mark.parametrize("body", [b"\x00", b"x" * 1025, b"\xff"])
def test_invalid_symlink_body_is_skipped_without_losing_other_files(body: bytes) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith(f"/src/{SHA}/"):
            return listing(file_entry("link", len(body), attributes=["link"]), file_entry("ok.py"))
        if request.url.path.endswith("/ok.py"):
            return httpx.Response(200, content=b"x")
        return httpx.Response(200, content=body)

    async def run() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            snapshot = source(client)
            archive = await snapshot.archive(SHA, max_bytes=10000)
            scan = await TarArchiveReader(IndexLimits()).scan(
                archive, documents=set(), deadline=time.monotonic() + 5
            )
            assert set(scan.entries) == {"ok.py"}
            assert archive.provider_skips == {"invalid_symlink": 1}

    asyncio.run(run())


def test_redirecting_raw_file_is_skipped_without_auth_leak() -> None:
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith(f"/src/{SHA}/"):
            return listing(file_entry("redirect.py"), file_entry("safe.py"))
        if request.url.path.endswith("redirect.py"):
            return httpx.Response(302, headers={"Location": "https://evil.example/steal"})
        return httpx.Response(200, content=b"x")

    async def run() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            snapshot = source(client)
            archive = await snapshot.archive(SHA, max_bytes=10000)
            scan = await TarArchiveReader(IndexLimits()).scan(
                archive, documents=set(), deadline=time.monotonic() + 5
            )
            assert set(scan.entries) == {"safe.py"}
            assert snapshot.skipped_entries == {"redirect": 1}
            assert archive.provider_skips == {"redirect": 1}

    asyncio.run(run())
    assert len(requests) == 3
    assert all(request.url.host == "api.bitbucket.org" for request in requests)


def test_static_token_401_is_terminal_and_preserves_status() -> None:
    calls = 0

    def handle(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(401)

    async def run() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            for _ in range(2):
                with pytest.raises(VcsAccessDenied, match="401"):
                    await source(client).head()

    asyncio.run(run())
    assert calls == 2


@pytest.mark.parametrize("status", [301, 500])
def test_http_diagnostic_preserves_status(status: int) -> None:
    async def run() -> None:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _: httpx.Response(status))
        ) as client:
            with pytest.raises(VcsUnavailable, match=str(status)):
                await source(client).head()

    asyncio.run(run())


def test_unrepresentable_and_subrepository_entries_are_skipped() -> None:
    raw_paths: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith(f"/src/{SHA}/"):
            return listing(
                file_entry("bad\\name.py"),
                file_entry("bad\x00name.py"),
                file_entry("x" * 1025),
                file_entry("module", kind="commit_directory"),
                file_entry("vendor", kind="commit_file", attributes=["subrepository"]),
                file_entry("nested", kind="commit_subrepository"),
                file_entry("ok.py"),
            )
        if request.url.path.endswith("/module/"):
            return listing()
        raw_paths.append(request.url.path)
        return httpx.Response(200, content=b"x")

    async def run() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            snapshot = source(client)
            archive = await snapshot.archive(SHA, max_bytes=10000)
            scan = await TarArchiveReader(IndexLimits()).scan(
                archive, documents=set(), deadline=time.monotonic() + 5
            )
            assert set(scan.entries) == {"ok.py"}
            assert snapshot.skipped_entries == {"unrepresentable_path": 3, "subrepository": 2}
            assert archive.provider_skips == snapshot.skipped_entries

    asyncio.run(run())
    assert len(raw_paths) == 1
