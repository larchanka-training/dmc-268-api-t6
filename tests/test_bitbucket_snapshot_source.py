"""Bitbucket index snapshots use immutable UUIDs and bounded fixed-origin reads."""

from __future__ import annotations

import asyncio
import time
from uuid import UUID

import httpx
import pytest

from app.modules.code_index.application.vcs import (
    Ancestry,
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
) -> BitbucketRepositorySnapshotSource:
    async def target() -> BoundBitbucketTarget:
        return _TARGET

    return BitbucketRepositorySnapshotSource(
        client=client,
        target_for_repository=target,
        credentials=credentials,
    )


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


def test_redirect_fails_closed_without_following() -> None:
    """A source redirect cannot yield a partial clean index or leak a token."""

    def handle(request: httpx.Request) -> httpx.Response:
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
            with pytest.raises(VcsUnavailable):
                await source.archive(_SHA, max_bytes=100_000)
            assert source.request_count == 2

    asyncio.run(exercise())


def test_compare_unknown_ancestry_forces_full_rebuild() -> None:
    """Without a provider ancestry proof, the indexer must not choose incremental mode."""

    async def exercise() -> None:
        credentials = _Credentials()
        transport = httpx.MockTransport(lambda _: httpx.Response(500))
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
