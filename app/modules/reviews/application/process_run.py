"""Producer-facing orchestration for immutable review-run inputs."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol, cast
from uuid import UUID

from app.common.application.unit_of_work import UnitOfWork
from app.modules.reviews.application.check_runs import RunProviderCheckpoint
from app.modules.reviews.application.conventions import (
    ActiveConventionsPrompt,
    GeneratedConventions,
    GenerateRepoConventions,
)
from app.modules.reviews.application.get_run_diff import (
    DiffSnapshot,
    DiffSnapshotRepository,
    StoreDiffSnapshot,
    is_proven_empty_snapshot,
    review_files_from_snapshots,
)
from app.modules.reviews.application.get_run_file_lines import (
    BLOB_CACHE_TTL,
    BlobCacheKey,
    BlobCacheStatus,
    BlobCacheWriter,
)
from app.modules.reviews.application.policy_errors import PolicyError, PolicyErrorCode
from app.modules.reviews.application.policy_snapshot import PolicySnapshot
from app.modules.reviews.application.prompt_builder import ReviewRule
from app.modules.reviews.application.review_path import (
    ReviewBinding,
    require_servable_without_snapshot,
    require_served_provider,
)
from app.modules.reviews.application.run_failures import (
    RequiredSourceUnavailable,
    RunCancelled,
    RunFailure,
)
from app.modules.reviews.application.run_trace import RunTrace, traced_step
from app.modules.reviews.application.vcs_diff import (
    MAX_BLOB_BYTES,
    FetchVcsReviewInput,
    PullRequestLocator,
    VcsProvider,
    VcsProviderSelector,
    VcsStageVerifier,
)

_LOGGER = logging.getLogger(__name__)


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
    """Detached routing and pinned refs; base_sha is the target/start, not the merge base."""

    code_change_id: UUID
    repository_id: UUID
    head_sha: str
    base_sha: str
    locator: PullRequestLocator
    base_ref: str | None = None

    @property
    def provider(self) -> str:
        return self.locator.provider

    @property
    def origin_provider_installation_id(self) -> UUID | None:
        return self.locator.origin_provider_installation_id

    @property
    def repository_external_id(self) -> int | None:
        return self.locator.repository_external_id


@dataclass(frozen=True)
class RunConventionsInput:
    repository_id: UUID
    conventions_prompt: ActiveConventionsPrompt
    rules: tuple[ReviewRule, ...] = ()
    # The review prompt the run pinned; None for a source that reads no pin.
    review_binding: ReviewBinding | None = None
    # The conventions prompt the run pinned (``Run.conventions_prompt_version_id``); a
    # snapshot-path run uses it instead of the active row. None when the run pins none.
    pinned_conventions_prompt: ActiveConventionsPrompt | None = None


class RunProcessingRepository(DiffSnapshotRepository, Protocol):
    async def get_run_diff_input(self, run_id: UUID) -> RunDiffInput | None: ...


class RunConventionsRepository(Protocol):
    async def get_run_conventions_input(self, run_id: UUID) -> RunConventionsInput | None: ...


class RunVcsRepository(Protocol):
    async def get_run_vcs_input(self, run_id: UUID) -> RunVcsInput | None: ...

    async def get_run_snapshots(self, run_id: UUID) -> list[DiffSnapshot] | None: ...


class ReviewContextStagePort(Protocol):
    """The policy and context stage of an attempt."""

    @property
    def policy_key(self) -> str | None: ...

    @property
    def snapshot_for_llm(self) -> PolicySnapshot | None: ...

    async def begin(self, run_id: UUID) -> None: ...

    async def run(self, run_id: UUID) -> object:
        """Enforce the attempt's policy, then build its context.

        On the snapshot path a change of technical skips only raises ``NoReviewableText``
        after its ``context.build`` (#130 F5). ``prepare`` lets it through; only
        ``ExecuteReviewRun`` handles it, around ``prepare``.
        """
        ...

    def excluded(self, path: str) -> bool: ...

    def excludes_change(self, path: str, previous_path: str | None) -> bool: ...


class RunProcessingUnitOfWork(UnitOfWork, Protocol):
    @property
    def repository(self) -> RunProcessingRepository: ...

    @property
    def blob_cache(self) -> BlobCacheWriter | None: ...


class ReviewRunProcessor:
    """Persist provider inputs before downstream review actions consume them."""

    def __init__(
        self,
        repository: RunProcessingRepository,
        provider: RunDiffProvider | None,
        blob_cache: BlobCacheWriter | None = None,
        conventions: GenerateRepoConventions | None = None,
        vcs_provider: VcsProvider | None = None,
        trace: RunTrace | None = None,
        *,
        uow_factory: Callable[[], RunProcessingUnitOfWork],
        context_stage: ReviewContextStagePort | None = None,
        vcs_provider_selector: VcsProviderSelector | None = None,
    ) -> None:
        self._repository = repository
        self._provider = provider
        self._blob_cache = blob_cache
        self._conventions = conventions
        self._vcs_provider = vcs_provider
        self._vcs_provider_selector = vcs_provider_selector
        self._trace = trace
        self._uow_factory = uow_factory
        self._context_stage = context_stage

    async def execute(self, run_id: UUID) -> bool:
        """Fetch and snapshot the exact head associated with a durable run.

        The worker never calls it: ``ExecuteReviewRun`` calls ``prepare``. With a context
        stage, ``NoReviewableText`` (a snapshot-path change of technical skips only, #130 F5)
        leaves this method unhandled; only ``ExecuteReviewRun`` handles it, around
        ``prepare``, and ``classify_failure`` would make it a retryable ``internal_error``.
        """

        return await self.prepare(run_id) is not None

    async def prepare(self, run_id: UUID) -> GeneratedConventions | bool | None:
        """Persist inputs and return the exact conventions snapshot for this run."""

        vcs_run: RunVcsInput | None = None
        failed_blob_shas: frozenset[str] = frozenset()
        conventions_input: RunConventionsInput | None = None
        # The snapshot path's conventions prompt, set once the policy load names it.
        pinned_prompt: ActiveConventionsPrompt | None = None
        vcs_provider = self._vcs_provider
        if vcs_provider is not None or self._vcs_provider_selector is not None:
            vcs_repository = cast(RunVcsRepository, self._repository)
            vcs_run = await vcs_repository.get_run_vcs_input(run_id)
            if vcs_run is None:
                return None
            if self._vcs_provider_selector is not None:
                vcs_provider = self._vcs_provider_selector.select(vcs_run.locator)
            elif vcs_run.provider != "github":
                raise ValueError(f"no VCS provider configured for {vcs_run.provider}")
            if vcs_provider is None:
                raise ValueError(f"no VCS provider configured for {vcs_run.provider}")
            if self._conventions is not None:
                # Before the policy load: the refusal needs only the run's binding.
                conventions_input = await self._servable_conventions_input(run_id)
                if conventions_input is None:
                    return None
            excluded: Callable[[DiffSnapshot], bool] | None = None
            if self._context_stage is not None:
                # The policy comes first: it decides what the diff threshold counts.
                await self._context_stage.begin(run_id)
                stage = self._context_stage
                if conventions_input is not None and stage.snapshot_for_llm is not None:
                    # R1: the pinned prompt must be the one the snapshot names; checked once
                    # the snapshot is known, before any diff read or write.
                    pinned_prompt = _pinned_conventions_prompt(
                        conventions_input, stage.snapshot_for_llm
                    )

                def excluded(snapshot: DiffSnapshot) -> bool:
                    return stage.excludes_change(snapshot.filename, snapshot.previous_filename)

            stored_files = await vcs_repository.get_run_snapshots(run_id)
            if stored_files is None:
                request = {
                    "pr_number": vcs_run.locator.number,
                    "head_sha": vcs_run.head_sha,
                    "base_sha": vcs_run.base_sha,
                }
                async with traced_step(self._trace, run_id, "vcs.fetch_diff", request) as step:
                    fetched = await FetchVcsReviewInput(
                        vcs_provider,
                        run_id=run_id,
                        checkpoint=self._provider_checkpoint(run_id, vcs_run),
                    ).execute(vcs_run.locator, vcs_run.head_sha, vcs_run.base_sha)
                    failed_blob_shas = fetched.failed_blob_shas
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
                    async with self._uow_factory() as uow:
                        stored_files = await StoreDiffSnapshot(uow.repository).execute(
                            run_id=run_id,
                            code_change_id=vcs_run.code_change_id,
                            head_sha=vcs_run.head_sha,
                            files=files,
                            **({} if excluded is None else {"excluded": excluded}),
                        )
                        await uow.commit()
                    step.response = {
                        "files": [
                            {
                                "filename": file.filename,
                                "status": file.status,
                                "additions": file.additions,
                                "deletions": file.deletions,
                                "too_large": file.omission_reason == "too_large",
                            }
                            for file in stored_files
                        ],
                        "total_lines": sum(file.changes for file in stored_files),
                        "summary_only": any(file.summary_only for file in stored_files),
                    }
        else:
            legacy_run = await self._repository.get_run_diff_input(run_id)
            if legacy_run is None:
                return None
            run = legacy_run
            if self._conventions is not None:
                conventions_input = await self._servable_conventions_input(run_id)
                if conventions_input is None:
                    return None
            if self._provider is None:
                raise RuntimeError("a VCS provider is required to process review diffs")
            files = await self._provider.fetch_diff(
                code_change_id=run.code_change_id,
                head_sha=run.head_sha,
            )
            async with self._uow_factory() as uow:
                stored_files = await StoreDiffSnapshot(uow.repository).execute(
                    run_id=run_id,
                    code_change_id=run.code_change_id,
                    head_sha=run.head_sha,
                    files=files,
                )
                await uow.commit()
        if vcs_run is not None:
            assert vcs_provider is not None
            await self._store_vcs_blobs(
                run_id, vcs_run, stored_files, failed_blob_shas, vcs_provider
            )
        else:
            await self._store_file_blobs(run, files)
        changed_paths = tuple(file.path for file in review_files_from_snapshots(stored_files)[0])
        if self._context_stage is not None:
            # After the diff is stored and before the first model call: a policy error
            # or a fully ignored change ends the attempt here.
            await self._context_stage.run(run_id)
            changed_paths = tuple(
                path for path in changed_paths if not self._context_stage.excluded(path)
            )
        if self._conventions is not None:
            # CONTRACT: read and checked before the diff whenever conventions are configured.
            assert conventions_input is not None
            policy_key = None if self._context_stage is None else self._context_stage.policy_key
            snapshot = None if self._context_stage is None else self._context_stage.snapshot_for_llm
            conventions_prompt = conventions_input.conventions_prompt
            extra: dict[str, Any] = {}
            if policy_key is not None:
                extra["policy_key"] = policy_key
            if snapshot is not None:
                # R1: the snapshot path takes the pinned prompt and the snapshot's AGENTS.md;
                # a legacy run keeps the active row and the provider read.
                # Checked after ``begin`` above; a composition without a VCS provider loads the
                # policy in ``stage.run``, so its check comes here.
                conventions_prompt = (
                    pinned_prompt
                    if pinned_prompt is not None
                    else _pinned_conventions_prompt(conventions_input, snapshot)
                )
                extra["snapshot"] = snapshot
            return await self._conventions.execute(
                repository_id=conventions_input.repository_id,
                conventions_prompt=conventions_prompt,
                run_id=run_id,
                changed_files=changed_paths,
                rules=conventions_input.rules,
                **extra,
            )
        return True

    async def _servable_conventions_input(self, run_id: UUID) -> RunConventionsInput | None:
        """The run's conventions input, read before the policy load and the diff: a
        snapshot-path run this composition cannot serve is refused here, before any
        policy read, diff read and model call."""
        conventions_repository = cast(RunConventionsRepository, self._repository)
        conventions_input = await conventions_repository.get_run_conventions_input(run_id)
        binding = None if conventions_input is None else conventions_input.review_binding
        if binding is not None:
            if self._context_stage is None:
                # Without the policy stage there is no snapshot.
                require_servable_without_snapshot(binding)
            else:
                # The stage checks the provider it builds the diff map with (TL-13); the
                # run's own provider is checked here, before the stage loads the policy.
                require_served_provider(binding)
        return conventions_input

    def _provider_checkpoint(self, run_id: UUID, expected: RunVcsInput) -> RunProviderCheckpoint:
        async def checkpoint() -> None:
            repository = cast(RunVcsRepository, self._repository)
            if await repository.get_run_vcs_input(run_id) != expected:
                raise RunCancelled("installation_unavailable")

        return checkpoint

    async def _store_file_blobs(self, run: RunDiffInput, files: list[DiffSnapshot]) -> None:
        assert self._provider is not None
        blobs: list[tuple[BlobCacheKey, str]] = []
        for file in files:
            if run.repository_id is None or file.blob_sha is None:
                continue
            content = await self._provider.fetch_file_content(
                code_change_id=run.code_change_id,
                head_sha=run.head_sha,
                path=file.filename,
            )
            blobs.append(
                (
                    BlobCacheKey(
                        repository_id=run.repository_id,
                        blob_sha=file.blob_sha,
                    ),
                    content,
                )
            )
        if blobs:
            async with self._uow_factory() as uow:
                if uow.blob_cache is not None:
                    for key, content in blobs:
                        await uow.blob_cache.put(key, content, ttl=BLOB_CACHE_TTL)
                await uow.commit()

    async def _store_vcs_blobs(
        self,
        run_id: UUID,
        run: RunVcsInput,
        files: list[DiffSnapshot],
        failed_blob_shas: frozenset[str],
        vcs_provider: VcsProvider,
    ) -> None:
        blobs: list[tuple[BlobCacheKey, str]] = []
        for file in files:
            if (
                file.blob_sha is None
                or file.blob_sha in failed_blob_shas
                or file.omission_reason in {"too_large", "generated"}
            ):
                continue
            key = BlobCacheKey(run.repository_id, file.blob_sha)
            if (
                self._blob_cache is not None
                and (await self._blob_cache.get(key)).status is BlobCacheStatus.HIT
            ):
                continue
            if is_proven_empty_snapshot(file):
                blobs.append((key, ""))
                continue
            try:
                blob = await vcs_provider.get_blob(
                    run.locator, file.blob_sha, checkpoint=self._provider_checkpoint(run_id, run)
                )
            except (RunCancelled, RequiredSourceUnavailable):
                raise
            except Exception as exc:
                if isinstance(exc, RunFailure) and (
                    exc.rate_limited
                    or exc.error_code
                    in {
                        "deadline_exceeded",
                        "bitbucket_request_budget_exceeded",
                        "bitbucket_response_budget_exceeded",
                    }
                ):
                    raise
                # Blob cache is optional; a single unavailable file must not abort the Run.
                _LOGGER.warning(
                    "VCS blob cache fetch failed run_id=%s repository=%s pr_number=%s "
                    "file=%s blob_sha=%s",
                    run_id,
                    run.locator.repository_full_name,
                    run.locator.number,
                    file.filename,
                    file.blob_sha,
                    exc_info=True,
                )
                continue
            if len(blob) > MAX_BLOB_BYTES:
                continue
            try:
                content = blob.decode("utf-8")
            except UnicodeDecodeError:
                continue
            if "\x00" in content:
                continue
            blobs.append((key, content))

        if isinstance(vcs_provider, VcsStageVerifier):
            await vcs_provider.verify_current(checkpoint=self._provider_checkpoint(run_id, run))
        if blobs:
            async with self._uow_factory() as uow:
                if uow.blob_cache is not None:
                    for key, content in blobs:
                        await uow.blob_cache.put(key, content, ttl=BLOB_CACHE_TTL)
                await uow.commit()


def _pinned_conventions_prompt(
    conventions_input: RunConventionsInput, snapshot: PolicySnapshot
) -> ActiveConventionsPrompt:
    """The conventions prompt a snapshot-path run pinned, which its snapshot must name."""
    pinned = conventions_input.pinned_conventions_prompt
    if pinned is None or pinned.id != snapshot.prompt_bindings.conventions_id:
        raise PolicyError(
            PolicyErrorCode.SNAPSHOT_INTEGRITY_ERROR,
            "The policy snapshot names another conventions prompt than the run pinned",
        )
    return pinned
