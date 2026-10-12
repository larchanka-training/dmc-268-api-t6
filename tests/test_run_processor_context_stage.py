"""The run processor calls the policy stage between storing the diff and any model call."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import cast
from uuid import UUID

import pytest

from app.modules.reviews.application.conventions import (
    ActiveConventionsPrompt,
    CachedConventions,
    GeneratedConventions,
    GenerateRepoConventions,
)
from app.modules.reviews.application.get_run_diff import DiffSnapshot
from app.modules.reviews.application.get_run_file_lines import BlobCacheKey
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


class GitHub:
    async def get_pull_request(self, locator: PullRequestLocator) -> VcsPullRequest:
        meta = PullRequestMeta("PR", None, "octo", "feature", "main", (), 2, 2, 2, False, False)
        return VcsPullRequest(locator, HEAD, BASE, meta)

    async def get_diff(self, pull_request: VcsPullRequest) -> tuple[VcsFile, ...]:
        return (_file("src/new.py", "1" * 40), _file("generated/api.py", "2" * 40))

    async def get_blob(self, locator: PullRequestLocator, sha: str) -> bytes:
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

    async def execute(
        self,
        *,
        repository_id: UUID,
        conventions_prompt: ActiveConventionsPrompt,
        run_id: UUID,
        changed_files: tuple[str, ...],
        rules: tuple[object, ...],
    ) -> GeneratedConventions:
        self.events.append("conventions model")
        self.changed_files = changed_files
        return GeneratedConventions(
            CachedConventions(REPOSITORY_ID, None, RUN_ID, (), (), {}), None, False
        )


@dataclass
class Stage:
    events: list[str]
    skip: bool = False

    async def run(self, run_id: UUID) -> object:
        self.events.append("policy stage")
        if self.skip:
            raise RunSkipped("all_changes_ignored")
        return None

    def excluded(self, path: str) -> bool:
        return path.startswith("generated/")


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

    assert events == ["diff stored", "policy stage", "conventions model"]
    # An ignored file is not named to the conventions model either.
    assert conventions.changed_files == ("src/new.py",)


def test_skipped_run_never_reaches_a_model_call() -> None:
    events: list[str] = []

    with pytest.raises(RunSkipped):
        _prepare(Stage(events, skip=True), events)

    assert events == ["diff stored", "policy stage"]


def test_without_the_stage_the_processor_behaves_as_before() -> None:
    events: list[str] = []

    conventions = _prepare(None, events)

    assert events == ["diff stored", "conventions model"]
    assert conventions.changed_files == ("src/new.py", "generated/api.py")
