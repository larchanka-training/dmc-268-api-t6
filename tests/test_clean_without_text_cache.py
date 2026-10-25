"""Proven empty additions remain readable without fetching blob bytes (#213)."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from uuid import UUID

import pytest

from app.modules.reviews.application.check_runs import RunProviderCheckpoint
from app.modules.reviews.application.get_run_diff import DiffSnapshot
from app.modules.reviews.application.get_run_file_lines import (
    BlobCacheKey,
    FileLinesNotFound,
    GetRunFileLines,
)
from app.modules.reviews.application.process_run import ReviewRunProcessor, RunVcsInput
from app.modules.reviews.application.vcs_diff import PullRequestLocator, VcsFile
from app.modules.reviews.infrastructure.blob_cache import InMemoryBlobCache
from tests.test_clean_without_text import EmptyAdditionProvider, SnapshotRepository
from tests.trigger_uow import processing_uow
from tests.vcs_facts import EMPTY_BLOB_SHA as _EMPTY_BLOB

_RUN_ID = UUID(int=1)
_CODE_CHANGE_ID = UUID(int=2)
_REPOSITORY_ID = UUID(int=3)
_HEAD = "a" * 40
_BASE = "b" * 40

_PATH = "pkg/__init__.py"
_LOCATOR = PullRequestLocator(17, "octo/repo", 7)
_EMPTY_SNAPSHOT = DiffSnapshot(
    _PATH, None, blob_sha=_EMPTY_BLOB, status="added", omission_reason="empty_file"
)


class Repository(SnapshotRepository):
    async def get_run_vcs_input(self, run_id: UUID) -> RunVcsInput:
        return RunVcsInput(_CODE_CHANGE_ID, _REPOSITORY_ID, _HEAD, _BASE, _LOCATOR)

    async def get_run_snapshots(self, run_id: UUID) -> list[DiffSnapshot] | None:
        return None if self.rows == "[]" else await self.get_run_diff(run_id)

    async def get_run_diff_input(self, run_id: UUID) -> None:
        raise AssertionError("only the VCS path is used")

    async def get_run_file_key(self, run_id: UUID, path: str) -> BlobCacheKey | None:
        if run_id == _RUN_ID:
            for snapshot in await self.get_run_diff(run_id):
                if snapshot.filename == path and snapshot.blob_sha is not None:
                    return BlobCacheKey(_REPOSITORY_ID, snapshot.blob_sha)
        return None


def test_proven_empty_file_is_readable_after_processing_and_retry_without_blob_requests() -> None:
    async def scenario() -> None:
        provider = EmptyAdditionProvider(
            (VcsFile(_PATH, "added", _EMPTY_BLOB, None, 0, 0, 0, None, git_mode="100644"),)
        )
        repository = Repository()
        for _ in range(2):
            # Retry must recover the content from durable proof even after cache loss.
            cache = InMemoryBlobCache()
            processor = ReviewRunProcessor(
                repository,
                None,
                cache,
                vcs_provider=provider,
                uow_factory=processing_uow(repository, cache),
            )

            assert await processor.execute(_RUN_ID) is True
            page = await GetRunFileLines(repository, cache).execute(_RUN_ID, _PATH, 0, 10)

            assert page.total_lines == 0
            assert page.lines == []
            assert page.next_offset is None
            assert provider.blob_requests == []

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "snapshot",
    [
        replace(_EMPTY_SNAPSHOT, omission_reason="missing_patch"),
        replace(_EMPTY_SNAPSHOT, changes=1),
        replace(_EMPTY_SNAPSHOT, blob_sha="c" * 40),
        replace(_EMPTY_SNAPSHOT, patch="@@ -0,0 +1 @@\n+content"),
        replace(_EMPTY_SNAPSHOT, summary_only=True),
    ],
)
def test_unproven_empty_marker_cannot_synthesize_cached_file_content(
    snapshot: DiffSnapshot,
) -> None:
    class UnavailableBlobProvider(EmptyAdditionProvider):
        async def get_blob(
            self, locator: PullRequestLocator, sha: str, *, checkpoint: RunProviderCheckpoint
        ) -> bytes:
            await checkpoint()
            self.blob_requests.append(sha)
            raise OSError("blob content is unavailable")

    async def scenario() -> None:
        repository = Repository()
        await repository.store_diff_snapshots(_RUN_ID, _CODE_CHANGE_ID, _HEAD, [snapshot])
        cache = InMemoryBlobCache()
        provider = UnavailableBlobProvider(())
        processor = ReviewRunProcessor(
            repository,
            None,
            cache,
            vcs_provider=provider,
            uow_factory=processing_uow(repository, cache),
        )

        assert await processor.execute(_RUN_ID) is True
        with pytest.raises(FileLinesNotFound):
            await GetRunFileLines(repository, cache).execute(_RUN_ID, _PATH, 0, 10)

    asyncio.run(scenario())
