"""Shared Bitbucket snapshot source fakes."""

from __future__ import annotations

from uuid import UUID

import httpx

from app.modules.code_index.application.bitbucket_persistence import (
    RawBlobCheckpoint,
    SourceListingCheckpoint,
)
from app.modules.code_index.domain.limits import IndexLimits
from app.modules.code_index.infrastructure.bitbucket_snapshot_source import (
    BitbucketRepositorySnapshotSource,
    BitbucketSourceBudgets,
    PublishedManifestStore,
)
from app.modules.integrations.bitbucket.application.request_credentials import (
    BitbucketRequestCredential,
    BoundBitbucketTarget,
)

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
    budgets: BitbucketSourceBudgets | None = None,
    checkpoint: SourceListingCheckpoint | None = None,
    raw_checkpoint: RawBlobCheckpoint | None = None,
    published_manifest: PublishedManifestStore | None = None,
) -> BitbucketRepositorySnapshotSource:
    async def target() -> BoundBitbucketTarget:
        return _TARGET

    return BitbucketRepositorySnapshotSource(
        client=client,
        target_for_repository=target,
        credentials=credentials,
        limits=limits,
        budgets=budgets,
        checkpoint=checkpoint,
        raw_checkpoint=raw_checkpoint,
        published_manifest=published_manifest,
    )
