"""Bitbucket index snapshots use immutable UUIDs and bounded fixed-origin reads."""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from uuid import UUID

import httpx
import pytest

from app.modules.code_index.application.vcs import (
    Ancestry,
    IndexLimitExceeded,
    VcsAccessDenied,
    VcsUnavailable,
)
from app.modules.code_index.domain.limits import IndexLimits
from app.modules.code_index.infrastructure import bitbucket_snapshot_source
from app.modules.code_index.infrastructure.bitbucket_snapshot_source import (
    BitbucketRepositorySnapshotSource,
)
from app.modules.code_index.infrastructure.tar_archive import TarArchiveReader
from app.modules.integrations.bitbucket.application.request_credentials import (
    BitbucketRequestCredential,
    BoundBitbucketTarget,
)
from app.modules.reviews.application.policy_errors import PolicyError, PolicyErrorCode
from app.modules.reviews.application.policy_snapshot import DocumentPresence

_SHA = "a" * 40
_REPO_UUID = UUID("11111111-1111-4111-8111-111111111111")
_TARGET = BoundBitbucketTarget(
    UUID("22222222-2222-4222-8222-222222222222"),
    UUID("33333333-3333-4333-8333-333333333333"),
    UUID("44444444-4444-4444-8444-444444444444"),
    _REPO_UUID,
    2,
    3,
)
_PREFIX = f"/2.0/repositories/%7B%7D/%7B{_REPO_UUID}%7D"


class _Credentials:
    def __init__(self) -> None:
        self.active = True
        self.checks = 0

    async def credential_for(self, target: BoundBitbucketTarget) -> BitbucketRequestCredential:
        assert target == _TARGET
        if not self.active:
            raise ValueError("revoked")
        return BitbucketRequestCredential(target, "private-test-token")

    async def checkpoint(self, target: BoundBitbucketTarget) -> None:
        assert target == _TARGET
        self.checks += 1
        if not self.active:
            raise ValueError("revoked")


def _source(
    credentials: _Credentials,
    client: httpx.AsyncClient,
    limits: IndexLimits | None = None,
) -> BitbucketRepositorySnapshotSource:
    async def target() -> BoundBitbucketTarget:
        return _TARGET

    return BitbucketRepositorySnapshotSource(
        client=client,
        target_for_repository=target,
        credentials=credentials,
        limits=limits,
    )


def test_listing_limits_fail_before_any_raw_download() -> None:
    raw_reads = 0

    def handle(request: httpx.Request) -> httpx.Response:
        nonlocal raw_reads
        if request.url.path.endswith(f"/src/{_SHA}/"):
            return httpx.Response(
                200,
                json={
                    "size": 3,
                    "values": [
                        {
                            "path": f"f{i}.py",
                            "type": "commit_file",
                            "size": 1,
                            "commit": {"hash": _SHA},
                        }
                        for i in range(3)
                    ],
                },
            )
        raw_reads += 1
        return httpx.Response(200, content=b"x")

    async def exercise() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            source = _source(_Credentials(), client, IndexLimits(archive_entries=2))
            with pytest.raises(IndexLimitExceeded, match="archive_entries"):
                await source.archive(_SHA, max_bytes=100_000)

    asyncio.run(exercise())
    assert raw_reads == 0


def test_snapshot_429_http_date_keeps_retryability() -> None:
    retry_date = format_datetime(datetime.now(UTC) + timedelta(minutes=3), usegmt=True)

    def handle(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith(f"/src/{_SHA}/")
        return httpx.Response(429, headers={"Retry-After": retry_date})

    async def exercise() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            source = _source(_Credentials(), client)
            with pytest.raises(VcsUnavailable) as error:
                await source.archive(_SHA, max_bytes=100_000)
            assert error.value.rate_limited
            assert error.value.retry_after is not None
            assert 170 <= error.value.retry_after.total_seconds() <= 180

    asyncio.run(exercise())


def test_snapshot_429_without_delay_pauses_for_an_hour() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429)

    async def exercise() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            source = _source(_Credentials(), client)
            with pytest.raises(VcsUnavailable) as error:
                await source.archive(_SHA, max_bytes=100_000)
            assert error.value.rate_limited
            assert error.value.retry_after == timedelta(hours=1)

    asyncio.run(exercise())


def test_rules_document_429_keeps_policy_retry_delay() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith(f"/src/{_SHA}/")
        return httpx.Response(429, headers={"Retry-After": "120"})

    async def exercise() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            source = _source(_Credentials(), client)
            with pytest.raises(PolicyError) as error:
                await source.rules_document(_SHA)
            assert error.value.code is PolicyErrorCode.RATE_LIMITED
            assert error.value.retry_after == timedelta(minutes=2)

    asyncio.run(exercise())


def test_rules_document_401_is_terminal_and_keeps_http_status() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401)

    async def exercise() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            source = _source(_Credentials(), client)
            with pytest.raises(PolicyError) as error:
                await source.rules_document(_SHA)
            assert error.value.code is PolicyErrorCode.ACCESS_DENIED
            assert "HTTP 401" in error.value.safe_message
            assert error.value.pin_head

    asyncio.run(exercise())


@pytest.mark.parametrize("status", [301, 302])
def test_rules_document_redirect_is_terminal_and_never_follows(status: int) -> None:
    calls: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.url.path.endswith(f"/src/{_SHA}/"):
            return httpx.Response(
                200,
                json={
                    "values": [
                        {"path": ".review", "type": "commit_directory", "commit": {"hash": _SHA}}
                    ]
                },
            )
        if request.url.path.endswith(f"/src/{_SHA}/.review/"):
            return httpx.Response(
                200,
                json={
                    "values": [
                        {
                            "path": ".review/rules.md",
                            "type": "commit_file",
                            "size": 1,
                            "commit": {"hash": _SHA},
                        }
                    ]
                },
            )
        return httpx.Response(status, headers={"Location": "https://evil.example/rules"})

    async def exercise() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            source = _source(_Credentials(), client)
            with pytest.raises(PolicyError) as error:
                await source.rules_document(_SHA)
            assert error.value.code is PolicyErrorCode.DOCUMENT_TYPE_UNSUPPORTED
            assert error.value.http_status == status
            assert f"HTTP {status}" in error.value.safe_message
            assert not error.value.pin_head

    asyncio.run(exercise())
    assert len(calls) == 3
    assert all(request.url.host == "api.bitbucket.org" for request in calls)


def test_archive_preflights_total_size_before_raw_reads() -> None:
    raw_reads = 0

    def handle(request: httpx.Request) -> httpx.Response:
        nonlocal raw_reads
        if request.url.path.endswith(f"/src/{_SHA}/"):
            return httpx.Response(
                200,
                json={
                    "size": 2,
                    "values": [
                        {
                            "path": f"f{i}.py",
                            "type": "commit_file",
                            "size": 2,
                            "commit": {"hash": _SHA},
                        }
                        for i in range(2)
                    ],
                },
            )
        raw_reads += 1
        return httpx.Response(200, content=b"xx")

    async def exercise() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            source = _source(_Credentials(), client)
            with pytest.raises(IndexLimitExceeded, match="download_bytes"):
                await source.archive(_SHA, max_bytes=3)

    asyncio.run(exercise())
    assert raw_reads == 0


def test_archive_preflights_remaining_request_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.modules.code_index.infrastructure import bitbucket_snapshot_source

    monkeypatch.setattr(bitbucket_snapshot_source, "_MAX_REQUESTS", 2)
    raw_reads = 0

    def handle(request: httpx.Request) -> httpx.Response:
        nonlocal raw_reads
        if request.url.path.endswith(f"/src/{_SHA}/"):
            return httpx.Response(
                200,
                json={
                    "size": 2,
                    "values": [
                        {
                            "path": f"f{i}.py",
                            "type": "commit_file",
                            "size": 1,
                            "commit": {"hash": _SHA},
                        }
                        for i in range(2)
                    ],
                },
            )
        raw_reads += 1
        return httpx.Response(200, content=b"x")

    async def exercise() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            source = _source(_Credentials(), client)
            with pytest.raises(IndexLimitExceeded, match="archive_entries"):
                await source.archive(_SHA, max_bytes=100_000)

    asyncio.run(exercise())
    assert raw_reads == 0


def test_changed_head_large_repository_stops_before_raw_downloads() -> None:
    """The bounded full fallback must spend one listing, not hundreds of raw reads."""
    base = "b" * 40
    paths: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.url.path.endswith(f"/merge-base/{base}..{_SHA}"):
            return httpx.Response(200, json={"hash": base})
        if request.url.path.endswith(f"/diffstat/{base}..{_SHA}"):
            return httpx.Response(
                200,
                json={
                    "size": 1,
                    "values": [
                        {
                            "status": "modified",
                            "old": {"path": "f000.py"},
                            "new": {"path": "f000.py"},
                        }
                    ],
                },
            )
        if request.url.path.endswith(f"/src/{_SHA}/"):
            return httpx.Response(
                200,
                json={
                    "size": 200,
                    "values": [
                        {
                            "path": f"f{i:03}.py",
                            "type": "commit_file",
                            "size": 1,
                            "commit": {"hash": _SHA},
                        }
                        for i in range(100)
                    ],
                    "next": f"https://api.bitbucket.org{_PREFIX}/src/{_SHA}/?page=2",
                },
            )
        raise AssertionError(request.url.path)

    async def exercise() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            source = _source(_Credentials(), client)
            comparison = await source.compare(base, _SHA)
            assert comparison is not None and comparison.complete
            with pytest.raises(IndexLimitExceeded, match="archive_entries"):
                await source.archive(_SHA, max_bytes=100_000)
            assert source.request_count == 3

    asyncio.run(exercise())
    assert len(paths) == 3


def test_rules_lookup_preserves_repository_size_limit() -> None:
    """The index reads rules first, so an oversize tree must keep its index diagnostic."""
    calls = 0

    def handle(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        assert request.url.path.endswith(f"/src/{_SHA}/")
        return httpx.Response(200, json={"size": 200, "values": []})

    async def exercise() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            source = _source(_Credentials(), client)
            with pytest.raises(IndexLimitExceeded, match="archive_entries"):
                await source.rules_document(_SHA)
            assert source.request_count == 1

    asyncio.run(exercise())
    assert calls == 1


def test_changed_head_supported_repository_has_measurable_full_snapshot_budget() -> None:
    """A proven ahead compare still fetches every eligible file within the temporary cap."""
    base = "b" * 40
    paths: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        paths.append(path)
        if path.endswith(f"/merge-base/{base}..{_SHA}"):
            return httpx.Response(200, json={"hash": base})
        if path.endswith(f"/diffstat/{base}..{_SHA}"):
            return httpx.Response(
                200,
                json={
                    "size": 1,
                    "values": [
                        {
                            "status": "modified",
                            "old": {"path": "f000.py"},
                            "new": {"path": "f000.py"},
                        }
                    ],
                },
            )
        if path.endswith(f"/src/{_SHA}/"):
            return httpx.Response(
                200,
                json={
                    "size": 100,
                    "values": [
                        {
                            "path": f"f{i:03}.py",
                            "type": "commit_file",
                            "size": 1,
                            "commit": {"hash": _SHA},
                        }
                        for i in range(100)
                    ],
                },
            )
        if f"/src/{_SHA}/f" in path:
            return httpx.Response(200, content=b"x")
        raise AssertionError(path)

    async def exercise() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            source = _source(_Credentials(), client)
            comparison = await source.compare(base, _SHA)
            assert comparison is not None and comparison.complete
            archive = await source.archive(_SHA, max_bytes=100_000)
            assert archive.commit_sha == _SHA
            assert source.request_count == 103

    asyncio.run(exercise())
    assert len(paths) == 103
    assert sum(f"/src/{_SHA}/f" in path for path in paths) == 100


def test_snapshot_skips_binary_lfs_and_files_over_configured_read_limit() -> None:
    raw_paths: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith(f"/src/{_SHA}/"):
            values = [
                {"path": "small.py", "type": "commit_file", "size": 1, "commit": {"hash": _SHA}},
                {"path": "large.py", "type": "commit_file", "size": 3, "commit": {"hash": _SHA}},
                {
                    "path": "asset.bin",
                    "type": "commit_file",
                    "size": 1,
                    "attributes": ["binary"],
                    "commit": {"hash": _SHA},
                },
                {
                    "path": "asset.lfs",
                    "type": "commit_file",
                    "size": 1,
                    "attributes": ["lfs"],
                    "commit": {"hash": _SHA},
                },
            ]
            return httpx.Response(200, json={"size": 4, "values": values})
        raw_paths.append(request.url.path)
        return httpx.Response(200, content=b"x")

    async def exercise() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            source = _source(_Credentials(), client, IndexLimits(read_bytes=2))
            archive = await source.archive(_SHA, max_bytes=100_000)
            scanned = await TarArchiveReader(IndexLimits()).scan(
                archive, documents=set(), deadline=time.monotonic() + 5
            )
            assert set(scanned.entries) == {"small.py"}
            assert archive.provider_skips == {"binary": 1, "lfs": 1}

    asyncio.run(exercise())
    assert raw_paths == [f"{_PREFIX}/src/{_SHA}/small.py".replace("%7B", "{").replace("%7D", "}")]


def test_compare_requires_merge_base_proof_before_incremental_files() -> None:
    base = "b" * 40
    seen: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        if request.url.path.endswith(f"/merge-base/{base}..{_SHA}"):
            return httpx.Response(200, json={"hash": base})
        if request.url.path.endswith(f"/diffstat/{base}..{_SHA}"):
            return httpx.Response(
                200,
                json={
                    "size": 1,
                    "values": [
                        {"status": "modified", "old": {"path": "app.py"}, "new": {"path": "app.py"}}
                    ],
                },
            )
        raise AssertionError(request.url.path)

    async def exercise() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            source = _source(_Credentials(), client)
            comparison = await source.compare(base, _SHA)
            assert comparison is not None
            assert comparison.ancestry is Ancestry.AHEAD
            assert comparison.complete
            assert [item.path for item in comparison.files] == ["app.py"]

    asyncio.run(exercise())
    assert len(seen) == 2


def test_compare_429_does_not_start_full_walk() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        assert "/merge-base/" in request.url.path
        return httpx.Response(429, headers={"Retry-After": "3600"})

    async def exercise() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            source = _source(_Credentials(), client)
            with pytest.raises(VcsUnavailable) as error:
                await source.compare("b" * 40, _SHA)
            assert error.value.rate_limited
            assert error.value.retry_after == timedelta(hours=1)
            assert source.request_count == 1

    asyncio.run(exercise())


def test_head_uses_uuid_and_rejects_renamed_locator() -> None:
    """The mutable workspace/slug is never an HTTP authority for an index job."""

    def handle(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "api.bitbucket.org"
        assert request.url.path.startswith(_PREFIX.replace("%7B", "{").replace("%7D", "}"))
        assert request.headers["Authorization"] == "Bearer private-test-token"
        if request.url.path.endswith(str(_REPO_UUID) + "}/"):
            return httpx.Response(
                200,
                json={"uuid": "{" + str(_REPO_UUID) + "}", "mainbranch": {"name": "main"}},
            )
        return httpx.Response(200, json={"name": "main", "target": {"hash": _SHA}})

    async def exercise() -> None:
        credentials = _Credentials()
        transport = httpx.MockTransport(handle)
        async with httpx.AsyncClient(transport=transport) as client:
            source = _source(credentials, client)
            head = await source.head()
            assert head is not None
            assert (head.branch, head.commit_sha) == ("main", _SHA)
            assert source.request_count == 2
            assert credentials.checks >= 2

    asyncio.run(exercise())


def test_opaque_page_archive_and_rules_are_pinned() -> None:
    """A complete recursive /src listing, including opaque next, produces the archive."""

    seen: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if request.url.path.endswith(f"/src/{_SHA}/"):
            if "cursor" in request.url.params:
                return httpx.Response(
                    200,
                    json={
                        "values": [
                            {
                                "path": "main.py",
                                "type": "commit_file",
                                "size": 9,
                                "commit": {"hash": _SHA},
                            }
                        ]
                    },
                )
            return httpx.Response(
                200,
                json={
                    "values": [
                        {
                            "path": "AGENTS.md",
                            "type": "commit_file",
                            "size": 6,
                            "commit": {"hash": _SHA},
                        }
                    ],
                    "next": f"https://api.bitbucket.org{_PREFIX}/src/{_SHA}/?cursor=opaque%2Bvalue",
                },
            )
        if request.url.path.endswith("/AGENTS.md"):
            return httpx.Response(200, content=b"Rules\n")
        if request.url.path.endswith("/main.py"):
            return httpx.Response(200, content=b"print(1)\n")
        raise AssertionError(str(request.url))

    async def exercise() -> None:
        credentials = _Credentials()
        transport = httpx.MockTransport(handle)
        async with httpx.AsyncClient(transport=transport) as client:
            source = _source(credentials, client)
            rules = await source.rules_document(_SHA)
            assert rules.presence is DocumentPresence.MISSING
            archive = await source.archive(_SHA, max_bytes=100_000)
            assert archive.commit_sha == _SHA
            assert archive.content.startswith(b"\x1f\x8b")
            scanned = await TarArchiveReader(IndexLimits()).scan(
                archive, documents={"AGENTS.md"}, deadline=time.monotonic() + 5
            )
            assert set(scanned.entries) == {"AGENTS.md", "main.py"}
            assert scanned.documents["AGENTS.md"] == b"Rules\n"
            assert any("cursor=opaque%2Bvalue" in url for url in seen)
            assert all(url.startswith("https://api.bitbucket.org") for url in seen)
            assert credentials.checks >= 4

    asyncio.run(exercise())


def test_redirect_is_skipped_without_following() -> None:
    """A raw-file redirect omits that file and never sends a token to another host."""

    seen: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if request.url.path.endswith(f"/src/{_SHA}/"):
            return httpx.Response(
                200,
                json={
                    "values": [
                        {
                            "path": "large.py",
                            "type": "commit_file",
                            "size": 9,
                            "commit": {"hash": _SHA},
                        }
                    ]
                },
            )
        return httpx.Response(302, headers={"Location": "https://evil.example/private"})

    async def exercise() -> None:
        credentials = _Credentials()
        transport = httpx.MockTransport(handle)
        async with httpx.AsyncClient(transport=transport) as client:
            source = _source(credentials, client)
            archive = await source.archive(_SHA, max_bytes=100_000)
            scanned = await TarArchiveReader(IndexLimits()).scan(
                archive, documents=set(), deadline=time.monotonic() + 5
            )
            assert scanned.entries == {}
            assert source.skipped_entries == {"redirect": 1}
            assert source.request_count == 2

    asyncio.run(exercise())
    assert len(seen) == 2
    assert all(url.startswith("https://api.bitbucket.org") for url in seen)


def test_compare_unknown_ancestry_forces_full_rebuild() -> None:
    """Without a provider ancestry proof, the indexer must not choose incremental mode."""

    async def exercise() -> None:
        credentials = _Credentials()
        transport = httpx.MockTransport(lambda _: httpx.Response(200, json={"hash": "c" * 40}))
        async with httpx.AsyncClient(transport=transport) as client:
            source = _source(credentials, client)
            same = await source.compare(_SHA, _SHA)
            assert same is not None and same.ancestry is Ancestry.IDENTICAL
            different = await source.compare("b" * 40, _SHA)
            assert different is not None and not different.complete

    asyncio.run(exercise())


def test_branch_forbidden_does_not_look_like_an_empty_repository() -> None:
    """A lost credential must never publish an empty source snapshot."""

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith(str(_REPO_UUID) + "}/"):
            return httpx.Response(
                200,
                json={"uuid": "{" + str(_REPO_UUID) + "}", "mainbranch": {"name": "main"}},
            )
        return httpx.Response(403)

    async def exercise() -> None:
        credentials = _Credentials()
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            source = _source(credentials, client)
            with pytest.raises(VcsAccessDenied):
                await source.head()

    asyncio.run(exercise())


def test_revoked_binding_after_fetch_never_returns_archive() -> None:
    """A disconnect during HTTP invalidates even a complete provider response."""

    credentials = _Credentials()

    def handle(request: httpx.Request) -> httpx.Response:
        credentials.active = False
        return httpx.Response(200, json={"values": []})

    async def exercise() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            source = _source(credentials, client)
            with pytest.raises(VcsAccessDenied):
                await source.archive(_SHA, max_bytes=100_000)

    asyncio.run(exercise())


def test_foreign_next_url_is_rejected_before_any_second_request() -> None:
    """An opaque page link never changes the authority carrying the API token."""
    requests = 0

    def handle(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(
            200,
            json={
                "values": [],
                "next": f"https://evil.example/2.0/repositories/%7B%7D/%7B{_REPO_UUID}%7D/src/{_SHA}/?page=2",
            },
        )

    async def exercise() -> None:
        credentials = _Credentials()
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            source = _source(credentials, client)
            with pytest.raises(VcsUnavailable):
                await source.archive(_SHA, max_bytes=100_000)
            assert requests == 1

    asyncio.run(exercise())


def test_oversized_source_listing_is_unavailable_not_an_invalid_rules_file() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith(f"/src/{_SHA}/")
        return httpx.Response(200, headers={"Content-Length": str(2 * 1024 * 1024 + 1)})

    async def exercise() -> PolicyError:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            source = _source(_Credentials(), client)
            with pytest.raises(PolicyError) as caught:
                await source.rules_document(_SHA)
        return caught.value

    error = asyncio.run(exercise())

    assert error.code is PolicyErrorCode.FETCH_TRANSIENT
    assert error.retryable is True


def test_exhausted_total_source_budget_is_unavailable_not_invalid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(bitbucket_snapshot_source, "_MAX_TOTAL_SOURCE_BYTES", 10)

    async def exercise() -> PolicyError:
        transport = httpx.MockTransport(lambda _: httpx.Response(200, json={"values": []}))
        async with httpx.AsyncClient(transport=transport) as client:
            with pytest.raises(PolicyError) as caught:
                await _source(_Credentials(), client).rules_document(_SHA)
        return caught.value

    assert asyncio.run(exercise()).code is PolicyErrorCode.FETCH_TRANSIENT


def test_complete_empty_rules_listing_proves_missing() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith(f"/src/{_SHA}/")
        return httpx.Response(200, json={"values": [], "size": 0})

    async def exercise() -> DocumentPresence:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            return (await _source(_Credentials(), client).rules_document(_SHA)).presence

    assert asyncio.run(exercise()) is DocumentPresence.MISSING


def test_incomplete_rules_listing_cannot_prove_missing() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith(f"/src/{_SHA}/")
        return httpx.Response(200, json={"values": [], "size": 1})

    async def exercise() -> PolicyError:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            with pytest.raises(PolicyError) as caught:
                await _source(_Credentials(), client).rules_document(_SHA)
        return caught.value

    assert asyncio.run(exercise()).code is PolicyErrorCode.FETCH_TRANSIENT


def test_rules_listing_rate_limit_preserves_retry_after() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith(f"/src/{_SHA}/")
        return httpx.Response(429, headers={"Retry-After": "17"})

    async def exercise() -> PolicyError:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            source = _source(_Credentials(), client)
            with pytest.raises(PolicyError) as caught:
                await source.rules_document(_SHA)
        return caught.value

    error = asyncio.run(exercise())

    assert error.code is PolicyErrorCode.RATE_LIMITED
    assert error.retry_after == timedelta(seconds=17)


def test_declared_oversized_rules_file_remains_invalid() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith(f"/src/{_SHA}/"):
            return httpx.Response(
                200,
                json={
                    "values": [
                        {"path": ".review", "type": "commit_directory", "commit": {"hash": _SHA}}
                    ]
                },
            )
        if request.url.path.endswith(f"/src/{_SHA}/.review/"):
            return httpx.Response(
                200,
                json={
                    "values": [
                        {
                            "path": ".review/rules.md",
                            "type": "commit_file",
                            "size": 65_537,
                            "commit": {"hash": _SHA},
                        }
                    ]
                },
            )
        raise AssertionError(str(request.url))

    async def exercise() -> PolicyError:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            source = _source(_Credentials(), client)
            with pytest.raises(PolicyError) as caught:
                await source.rules_document(_SHA)
        return caught.value

    error = asyncio.run(exercise())

    assert error.code is PolicyErrorCode.DOCUMENT_TOO_LARGE
    assert error.limit == 65_536


def test_oversized_rules_response_body_remains_invalid() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith(f"/src/{_SHA}/"):
            return httpx.Response(
                200,
                json={
                    "values": [
                        {"path": ".review", "type": "commit_directory", "commit": {"hash": _SHA}}
                    ]
                },
            )
        if request.url.path.endswith(f"/src/{_SHA}/.review/"):
            return httpx.Response(
                200,
                json={
                    "values": [
                        {
                            "path": ".review/rules.md",
                            "type": "commit_file",
                            "size": 1,
                            "commit": {"hash": _SHA},
                        }
                    ]
                },
            )
        if request.url.path.endswith(f"/src/{_SHA}/.review/rules.md"):
            return httpx.Response(200, content=b"x" * 65_537)
        raise AssertionError(str(request.url))

    async def exercise() -> PolicyError:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            with pytest.raises(PolicyError) as caught:
                await _source(_Credentials(), client).rules_document(_SHA)
        return caught.value

    assert asyncio.run(exercise()).code is PolicyErrorCode.DOCUMENT_TOO_LARGE


@pytest.mark.parametrize(
    ("declared_size", "body"),
    [(20, b"# Rules\n"), (1, b"# Rules\n")],
    ids=["truncated-200", "longer-than-listing"],
)
def test_rules_file_size_mismatch_is_unavailable(declared_size: int, body: bytes) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith(f"/src/{_SHA}/"):
            return httpx.Response(
                200,
                json={
                    "values": [
                        {"path": ".review", "type": "commit_directory", "commit": {"hash": _SHA}}
                    ]
                },
            )
        if request.url.path.endswith(f"/src/{_SHA}/.review/"):
            return httpx.Response(
                200,
                json={
                    "values": [
                        {
                            "path": ".review/rules.md",
                            "type": "commit_file",
                            "size": declared_size,
                            "commit": {"hash": _SHA},
                        }
                    ]
                },
            )
        if request.url.path.endswith(f"/src/{_SHA}/.review/rules.md"):
            return httpx.Response(200, content=body)
        raise AssertionError(str(request.url))

    async def exercise() -> PolicyError:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            with pytest.raises(PolicyError) as caught:
                await _source(_Credentials(), client).rules_document(_SHA)
        return caught.value

    assert asyncio.run(exercise()).code is PolicyErrorCode.FETCH_TRANSIENT
