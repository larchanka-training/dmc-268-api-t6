"""Bitbucket RAG cached blobs are still bound to the current run and repository."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from uuid import UUID

import pytest

from app.bootstrap.bitbucket_reviews import _FencedGraphRetriever, _FencedRagBlobs, _RagFence
from app.modules.code_index.domain.rag_index import rag_index
from app.modules.integrations.bitbucket.application.request_credentials import (
    BitbucketRequestCredential,
    BitbucketTargetInactive,
    BoundBitbucketTarget,
)
from app.modules.reviews.application.get_run_file_lines import BlobCacheKey
from app.modules.reviews.application.graph_retriever import GraphHints, GraphTarget
from app.modules.reviews.application.run_failures import RunCancelled
from app.modules.reviews.infrastructure.blob_cache import InMemoryBlobCache

_SHA = "a" * 40
_TARGET = BoundBitbucketTarget(UUID(int=1), UUID(int=2), UUID(int=3), UUID(int=4), 1, 1)


class Authority:
    active = True

    async def credential_for(self, target: BoundBitbucketTarget) -> BitbucketRequestCredential:
        assert target == _TARGET
        return BitbucketRequestCredential(target, "token")

    async def checkpoint(self, target: BoundBitbucketTarget) -> None:
        assert target == _TARGET
        if not self.active:
            raise BitbucketTargetInactive()


class ForbiddenSource:
    async def read_blob(self, blob_sha: str) -> bytes:
        raise AssertionError(f"unexpected VCS blob read {blob_sha}")


def test_revoke_blocks_even_a_local_or_shared_blob_cache_hit() -> None:
    """A cached blob cannot bypass the run origin and credential generation fence."""

    async def scenario() -> None:
        authority = Authority()
        cache = InMemoryBlobCache()
        await cache.put(BlobCacheKey(_TARGET.repository_id, _SHA), "private content")

        async def checkpoint() -> None:
            if not authority.active:
                raise RunCancelled("installation_unavailable")

        source = _FencedRagBlobs(
            fence=_RagFence(authority, _TARGET, checkpoint),
            repository_id=_TARGET.repository_id,
            source=ForbiddenSource(),
            cache=cache,
            uow_factory=lambda: pytest.fail("cache hit must not create a unit of work"),
        )
        assert await source.text(_SHA) == "private content"
        authority.active = False
        with pytest.raises(RunCancelled):
            await source.text(_SHA)

    asyncio.run(scenario())


def test_revoke_during_graph_read_never_returns_foreign_hints() -> None:
    """Graph results are fenced after their DB read, before review context can use them."""

    async def scenario() -> None:
        authority = Authority()

        async def checkpoint() -> None:
            return None

        class Graph:
            async def read_hints(
                self, repository_id: UUID, targets: Sequence[GraphTarget]
            ) -> GraphHints:
                authority.active = False
                return GraphHints(
                    rag_index(
                        rag_enabled=False,
                        status=None,
                        indexed_commit_sha=None,
                        target_commit_sha=None,
                        published_at=None,
                        file_count=None,
                        symbol_count=None,
                        error_code=None,
                        last_error_code=None,
                    ),
                    (),
                )

        reader = _FencedGraphRetriever(
            inner=Graph(), fence=_RagFence(authority, _TARGET, checkpoint)
        )
        with pytest.raises(RunCancelled):
            await reader.read_hints(_TARGET.repository_id, ())
        authority.active = True
        with pytest.raises(RunCancelled):
            await reader.read_hints(UUID(int=99), ())

    asyncio.run(scenario())
