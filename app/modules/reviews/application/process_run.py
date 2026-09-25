"""Producer-facing orchestration for immutable review-run inputs."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, cast
from uuid import UUID

from app.modules.reviews.application.conventions import GenerateRepoConventions
from app.modules.reviews.application.get_run_diff import (
    DiffSnapshot,
    DiffSnapshotRepository,
    StoreDiffSnapshot,
)
from app.modules.reviews.application.get_run_file_lines import (
    BLOB_CACHE_TTL,
    BlobCacheKey,
    BlobCacheWriter,
)


class RunDiffProvider(Protocol):
    """The provider operation run processing uses before reviewing a head SHA."""

    async def fetch_diff(self, *, code_change_id: UUID, head_sha: str) -> list[DiffSnapshot]: ...

    async def fetch_file_content(
        self, *, code_change_id: UUID, head_sha: str, path: str
    ) -> str: ...


@dataclass(frozen=True)
class RunDiffInput:
    code_change_id: UUID
    head_sha: str


@dataclass(frozen=True)
class RunConventionsInput:
    repository_id: UUID
    prompt_version_id: UUID


class RunProcessingRepository(DiffSnapshotRepository, Protocol):
    async def get_run_diff_input(self, run_id: UUID) -> RunDiffInput | None: ...


class RunConventionsRepository(Protocol):
    async def get_run_conventions_input(self, run_id: UUID) -> RunConventionsInput | None: ...


class ReviewRunProcessor:
    """Persist provider inputs before downstream review actions consume them."""

    def __init__(
        self,
        repository: RunProcessingRepository,
        provider: RunDiffProvider,
        blob_cache: BlobCacheWriter | None = None,
        conventions: GenerateRepoConventions | None = None,
    ) -> None:
        self._repository = repository
        self._provider = provider
        self._blob_cache = blob_cache
        self._conventions = conventions

    async def execute(self, run_id: UUID) -> bool:
        """Fetch and snapshot the exact head associated with a durable run."""

        run = await self._repository.get_run_diff_input(run_id)
        if run is None:
            return False
        files = await self._provider.fetch_diff(
            code_change_id=run.code_change_id,
            head_sha=run.head_sha,
        )
        await StoreDiffSnapshot(self._repository).execute(
            code_change_id=run.code_change_id,
            head_sha=run.head_sha,
            files=files,
        )
        if self._blob_cache is not None:
            await self._store_file_blobs(run, files)
        if self._conventions is not None:
            conventions_repository = cast(RunConventionsRepository, self._repository)
            conventions_input = await conventions_repository.get_run_conventions_input(run_id)
            if conventions_input is None:
                return False
            await self._conventions.execute(
                repository_id=conventions_input.repository_id,
                prompt_version_id=conventions_input.prompt_version_id,
                run_id=run_id,
            )
        return True

    async def _store_file_blobs(self, run: RunDiffInput, files: list[DiffSnapshot]) -> None:
        assert self._blob_cache is not None
        for file in files:
            content = await self._provider.fetch_file_content(
                code_change_id=run.code_change_id,
                head_sha=run.head_sha,
                path=file.filename,
            )
            await self._blob_cache.put(
                BlobCacheKey(
                    code_change_id=run.code_change_id,
                    head_sha=run.head_sha,
                    path=file.filename,
                ),
                content,
                ttl=BLOB_CACHE_TTL,
            )
