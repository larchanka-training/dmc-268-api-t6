"""The run processor calls the policy stage between storing the diff and any model call."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import cast
from uuid import UUID

import pytest

from app.modules.reviews.application.check_runs import RunProviderCheckpoint
from app.modules.reviews.application.conventions import (
    ActiveConventionsPrompt,
    CachedConventions,
    GeneratedConventions,
    GenerateRepoConventions,
)
from app.modules.reviews.application.get_run_diff import (
    DiffSnapshot,
    review_files_from_snapshots,
)
from app.modules.reviews.application.get_run_file_lines import BlobCacheKey
from app.modules.reviews.application.policy_errors import PolicyError, PolicyErrorCode
from app.modules.reviews.application.policy_snapshot import PolicySnapshot
from app.modules.reviews.application.process_run import (
    ReviewRunProcessor,
    RunConventionsInput,
    RunVcsInput,
)
from app.modules.reviews.application.prompt_builder import PullRequestMeta
from app.modules.reviews.application.run_failures import RunSkipped
from app.modules.reviews.application.vcs_diff import PullRequestLocator, VcsFile, VcsPullRequest
from app.modules.reviews.infrastructure.blob_cache import InMemoryBlobCache
from tests.trigger_uow import processing_uow

RUN_ID = UUID("00000000-0000-0000-0000-000000000101")
CODE_CHANGE_ID = UUID("00000000-0000-0000-0000-000000000102")
REPOSITORY_ID = UUID("00000000-0000-0000-0000-000000000103")
HEAD = "a" * 40
BASE = "b" * 40
LOCATOR = PullRequestLocator(17, "octo/repo", 7)
_PATCH = "@@ -1 +1 @@\n-old\n+new"


def _file(path: str, sha: str) -> VcsFile:
    return VcsFile(path, "modified", sha, None, 1, 1, 2, _PATCH)


@dataclass
class GitHub:
    files: tuple[VcsFile, ...] = (
        _file("src/new.py", "1" * 40),
        _file("generated/api.py", "2" * 40),
    )

    async def get_pull_request(
        self, locator: PullRequestLocator, *, checkpoint: RunProviderCheckpoint
    ) -> VcsPullRequest:
        await checkpoint()
        meta = PullRequestMeta("PR", None, "octo", "feature", "main", (), 2, 2, 2, False, False)
        return VcsPullRequest(locator, HEAD, BASE, meta)

    async def get_diff(
        self, pull_request: VcsPullRequest, *, checkpoint: RunProviderCheckpoint
    ) -> tuple[VcsFile, ...]:
        await checkpoint()
        return self.files

    async def get_blob(
        self, locator: PullRequestLocator, sha: str, *, checkpoint: RunProviderCheckpoint
    ) -> bytes:
        await checkpoint()
        return b"new\n"


@dataclass
class Repository:
    events: list[str]
    snapshots: list[DiffSnapshot] = field(default_factory=list)

    async def get_run_diff_input(self, run_id: UUID) -> None:
        raise AssertionError("VCS path must use the full repository locator")

    async def get_run_vcs_input(self, run_id: UUID) -> RunVcsInput:
        return RunVcsInput(CODE_CHANGE_ID, REPOSITORY_ID, HEAD, BASE, LOCATOR)

    async def get_run_snapshots(self, run_id: UUID) -> None:
        return None

    async def store_diff_snapshots(
        self, run_id: UUID, code_change_id: UUID, head_sha: str, snapshots: list[DiffSnapshot]
    ) -> list[DiffSnapshot]:
        self.events.append("diff stored")
        self.snapshots = snapshots
        return snapshots

    async def get_run_file_key(self, run_id: UUID, path: str) -> BlobCacheKey | None:
        return None

    async def get_run_conventions_input(self, run_id: UUID) -> RunConventionsInput:
        return RunConventionsInput(REPOSITORY_ID, ActiveConventionsPrompt(RUN_ID, "prompt"))


@dataclass
class Conventions:
    events: list[str]
    changed_files: tuple[str, ...] | None = None
    policy_key: str | None = None

    async def execute(
        self,
        *,
        repository_id: UUID,
        conventions_prompt: ActiveConventionsPrompt,
        run_id: UUID,
        changed_files: tuple[str, ...],
        rules: tuple[object, ...],
        policy_key: str | None = None,
    ) -> GeneratedConventions:
        self.events.append("conventions model")
        self.changed_files = changed_files
        self.policy_key = policy_key
        return GeneratedConventions(
            CachedConventions(REPOSITORY_ID, None, RUN_ID, (), (), {}), None, False
        )


@dataclass
class Stage:
    events: list[str]
    skip: bool = False
    policy_key: str | None = "1" * 64
    # The snapshot the review is given: None keeps the run on the legacy path.
    snapshot_for_llm: PolicySnapshot | None = None

    async def begin(self, run_id: UUID) -> None:
        self.events.append("policy loaded")

    async def run(self, run_id: UUID) -> object:
        self.events.append("policy stage")
        if self.skip:
            raise RunSkipped("all_changes_ignored")
        return None

    def excluded(self, path: str) -> bool:
        return path.startswith(("generated/", "vendor/"))

    def excludes_change(self, path: str, previous_path: str | None) -> bool:
        return any(self.excluded(item) for item in (path, previous_path) if item)


def _prepare(stage: Stage | None, events: list[str]) -> Conventions:
    repository = Repository(events)
    conventions = Conventions(events)
    options = {} if stage is None else {"context_stage": stage}
    processor = ReviewRunProcessor(
        repository,
        None,
        blob_cache=InMemoryBlobCache(),
        conventions=cast(GenerateRepoConventions, conventions),
        vcs_provider=GitHub(),
        uow_factory=processing_uow(repository),
        **options,  # type: ignore[arg-type]
    )
    asyncio.run(processor.prepare(RUN_ID))
    return conventions


def test_stage_runs_after_the_diff_is_stored_and_before_the_conventions_model() -> None:
    events: list[str] = []

    conventions = _prepare(Stage(events), events)

    assert events == ["policy loaded", "diff stored", "policy stage", "conventions model"]
    assert conventions.policy_key == "1" * 64
    # An ignored file is not named to the conventions model either.
    assert conventions.changed_files == ("src/new.py",)


def test_skipped_run_never_reaches_a_model_call() -> None:
    events: list[str] = []

    with pytest.raises(RunSkipped):
        _prepare(Stage(events, skip=True), events)

    assert events == ["policy loaded", "diff stored", "policy stage"]


def test_without_the_stage_the_processor_behaves_as_before() -> None:
    events: list[str] = []

    conventions = _prepare(None, events)

    assert events == ["diff stored", "conventions model"]
    assert conventions.changed_files == ("src/new.py", "generated/api.py")
    assert conventions.policy_key is None


def _summary_only(*files: VcsFile) -> bool:
    events: list[str] = []
    repository = Repository(events)
    processor = ReviewRunProcessor(
        repository,
        None,
        blob_cache=InMemoryBlobCache(),
        conventions=cast(GenerateRepoConventions, Conventions(events)),
        vcs_provider=GitHub(files),
        uow_factory=processing_uow(repository),
        context_stage=Stage(events),
    )
    asyncio.run(processor.prepare(RUN_ID))
    return any(snapshot.summary_only for snapshot in repository.snapshots)


def _added(count: int, *, context: int = 0, patch: bool = True) -> VcsFile:
    lines = [f"+value_{n} = {n}" for n in range(count)] + [" kept"] * context
    body = f"@@ -1,{context} +1,{count + context} @@\n" + "\n".join(lines)
    return VcsFile(
        "src/app.py", "modified", "1" * 40, None, count, 0, count, body if patch else None
    )


def test_summary_only_threshold_counts_added_and_removed_lines_of_the_vcs() -> None:
    # Exactly 3 000 changed lines are still reviewed; the hunk header is not a change.
    assert _summary_only(_added(3000)) is False
    assert _summary_only(_added(3001)) is True
    # Context lines of a hunk are not changes either.
    assert _summary_only(_added(100, context=3500)) is False
    # A patch the provider did not send is not zero changes.
    assert _summary_only(_added(4000, patch=False)) is True


def test_policy_error_of_the_stage_ends_the_attempt_before_any_model_call() -> None:
    class Failing(Stage):
        async def run(self, run_id: UUID) -> object:
            self.events.append("policy stage")
            raise PolicyError(PolicyErrorCode.MATCH_BUDGET_EXCEEDED, "over the budget")

    events: list[str] = []

    with pytest.raises(PolicyError):
        _prepare(Failing(events), events)

    assert events == ["policy loaded", "diff stored", "policy stage"]


def test_ignored_lines_do_not_make_the_allowed_change_summary_only() -> None:
    # The normative case: 4 000 ignored lines next to 50 allowed ones.
    big = "@@ -0,0 +1,4000 @@\n" + "\n".join("+x = 1" for _ in range(4000))
    small = "@@ -0,0 +1,50 @@\n" + "\n".join(f"+value_{n} = {n}" for n in range(50))
    files = (
        VcsFile("vendor/client.py", "added", "2" * 40, None, 4000, 0, 4000, big),
        VcsFile("src/app.py", "added", "1" * 40, None, 50, 0, 50, small),
    )

    def stored(stage: Stage | None) -> list[DiffSnapshot]:
        events: list[str] = []
        repository = Repository(events)
        options = {} if stage is None else {"context_stage": stage}
        processor = ReviewRunProcessor(
            repository,
            None,
            blob_cache=InMemoryBlobCache(),
            conventions=cast(GenerateRepoConventions, Conventions(events)),
            vcs_provider=GitHub(files),
            uow_factory=processing_uow(repository),
            **options,  # type: ignore[arg-type]
        )
        asyncio.run(processor.prepare(RUN_ID))
        return repository.snapshots

    with_policy = stored(Stage([]))
    changed, omitted = review_files_from_snapshots(with_policy)
    # What the review request is built from: the allowed file with all its 50 lines.
    assert [(file.path, len(file.lines)) for file in changed] == [("src/app.py", 50)]
    assert omitted == ("vendor/client.py",)
    assert not any(snapshot.summary_only for snapshot in with_policy)
    vendor = next(item for item in with_policy if item.filename == "vendor/client.py")
    # The UI keeps the full patch of the ignored file; the model gets none of it.
    assert vendor.patch is not None and vendor.review_patch is None

    without_policy = stored(None)
    assert all(snapshot.summary_only for snapshot in without_policy)
    assert review_files_from_snapshots(without_policy)[0] == ()
