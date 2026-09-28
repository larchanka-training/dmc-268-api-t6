"""Producer-facing orchestration for immutable review-run inputs."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, cast
from uuid import UUID

from app.modules.reviews.application.conventions import (
    ActiveConventionsPrompt,
    GeneratedConventions,
    GenerateRepoConventions,
)
from app.modules.reviews.application.get_run_diff import (
    DiffSnapshot,
    DiffSnapshotRepository,
    StoreDiffSnapshot,
    review_files_from_snapshots,
)
from app.modules.reviews.application.get_run_file_lines import (
    BLOB_CACHE_TTL,
    BlobCacheKey,
    BlobCacheStatus,
    BlobCacheWriter,
)
from app.modules.reviews.application.prompt_builder import ReviewRule
from app.modules.reviews.application.vcs_diff import (
    FetchVcsReviewInput,
    PullRequestLocator,
    VcsProvider,
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
    repository_id: UUID | None = None


@dataclass(frozen=True)
class RunVcsInput:
    code_change_id: UUID
    repository_id: UUID
    head_sha: str
    base_sha: str
    locator: PullRequestLocator


@dataclass(frozen=True)
class RunConventionsInput:
    repository_id: UUID
    conventions_prompt: ActiveConventionsPrompt
    rules: tuple[ReviewRule, ...] = ()


class RunProcessingRepository(DiffSnapshotRepository, Protocol):
    async def get_run_diff_input(self, run_id: UUID) -> RunDiffInput | None: ...


class RunConventionsRepository(Protocol):
    async def get_run_conventions_input(self, run_id: UUID) -> RunConventionsInput | None: ...


class RunVcsRepository(Protocol):
    async def get_run_vcs_input(self, run_id: UUID) -> RunVcsInput | None: ...

    async def get_run_snapshots(self, run_id: UUID) -> list[DiffSnapshot] | None: ...


class ReviewRunProcessor:
    """Persist provider inputs before downstream review actions consume them."""

    def __init__(
        self,
        repository: RunProcessingRepository,
        provider: RunDiffProvider | None,
        blob_cache: BlobCacheWriter | None = None,
        conventions: GenerateRepoConventions | None = None,
        vcs_provider: VcsProvider | None = None,
    ) -> None:
        self._repository = repository
        self._provider = provider
        self._blob_cache = blob_cache
        self._conventions = conventions
        self._vcs_provider = vcs_provider

    async def execute(self, run_id: UUID) -> bool:
        """Fetch and snapshot the exact head associated with a durable run."""

        return await self.prepare(run_id) is not None

    async def prepare(self, run_id: UUID) -> GeneratedConventions | bool | None:
        """Persist inputs and return the exact conventions snapshot for this run."""

        vcs_run: RunVcsInput | None = None
        if self._vcs_provider is not None:
            vcs_repository = cast(RunVcsRepository, self._repository)
            vcs_run = await vcs_repository.get_run_vcs_input(run_id)
            if vcs_run is None:
                return None
            stored_files = await vcs_repository.get_run_snapshots(run_id)
            if stored_files is None:
                fetched = await FetchVcsReviewInput(self._vcs_provider).execute(
                    vcs_run.locator, vcs_run.head_sha, vcs_run.base_sha
                )
                omission_by_path = {
                    item.file.filename: item.reason.value for item in fetched.omissions
                }
                files = [
                    DiffSnapshot(
                        filename=file.filename,
                        patch=file.patch,
                        blob_sha=file.blob_sha,
                        status=file.status,
                        previous_filename=file.previous_filename,
                        additions=file.additions,
                        deletions=file.deletions,
                        changes=file.changes,
                        omission_reason=omission_by_path.get(file.filename),
                    )
                    for file in fetched.files
                ]
                stored_files = await StoreDiffSnapshot(self._repository).execute(
                    run_id=run_id,
                    code_change_id=vcs_run.code_change_id,
                    head_sha=vcs_run.head_sha,
                    files=files,
                )
        else:
            legacy_run = await self._repository.get_run_diff_input(run_id)
            if legacy_run is None:
                return None
            run = legacy_run
            if self._provider is None:
                raise RuntimeError("a VCS provider is required to process review diffs")
            files = await self._provider.fetch_diff(
                code_change_id=run.code_change_id,
                head_sha=run.head_sha,
            )
            stored_files = await StoreDiffSnapshot(self._repository).execute(
                run_id=run_id,
                code_change_id=run.code_change_id,
                head_sha=run.head_sha,
                files=files,
            )
        if self._blob_cache is not None:
            if self._vcs_provider is not None:
                assert vcs_run is not None
                await self._store_vcs_blobs(vcs_run, stored_files)
            else:
                await self._store_file_blobs(run, files)
        if self._conventions is not None:
            conventions_repository = cast(RunConventionsRepository, self._repository)
            conventions_input = await conventions_repository.get_run_conventions_input(run_id)
            if conventions_input is None:
                return None
            return await self._conventions.execute(
                repository_id=conventions_input.repository_id,
                conventions_prompt=conventions_input.conventions_prompt,
                run_id=run_id,
                changed_files=tuple(
                    file.path for file in review_files_from_snapshots(stored_files)[0]
                ),
                rules=conventions_input.rules,
            )
        return True

    async def _store_file_blobs(self, run: RunDiffInput, files: list[DiffSnapshot]) -> None:
        assert self._blob_cache is not None
        assert self._provider is not None
        for file in files:
            if run.repository_id is None or file.blob_sha is None:
                continue
            content = await self._provider.fetch_file_content(
                code_change_id=run.code_change_id,
                head_sha=run.head_sha,
                path=file.filename,
            )
            await self._blob_cache.put(
                BlobCacheKey(
                    repository_id=run.repository_id,
                    blob_sha=file.blob_sha,
                ),
                content,
                ttl=BLOB_CACHE_TTL,
            )

    async def _store_vcs_blobs(self, run: RunVcsInput, files: list[DiffSnapshot]) -> None:
        assert self._blob_cache is not None
        assert self._vcs_provider is not None
        for file in files:
            if file.blob_sha is None or file.omission_reason in {"too_large", "generated"}:
                continue
            key = BlobCacheKey(run.repository_id, file.blob_sha)
            if (await self._blob_cache.get(key)).status is BlobCacheStatus.HIT:
                continue
            blob = await self._vcs_provider.get_blob(run.locator, file.blob_sha)
            if len(blob) > 1_048_576:
                continue
            try:
                content = blob.decode("utf-8")
            except UnicodeDecodeError:
                continue
            if "\x00" in content:
                continue
            await self._blob_cache.put(key, content, ttl=BLOB_CACHE_TTL)
