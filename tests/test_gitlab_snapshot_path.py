"""GitLab v4 reaches the worker's real context, review and verifier pipeline."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import UUID

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import app.worker as worker
from app.modules.reviews.application.ast_resolution import RevisionTree
from app.modules.reviews.application.check_runs import RunProviderCheckpoint
from app.modules.reviews.application.conventions import GeneratedConventions
from app.modules.reviews.application.execute_review import ReviewPromptInput
from app.modules.reviews.application.get_run_diff import DiffSnapshot, review_files_from_snapshots
from app.modules.reviews.application.handle_review_run import ClaimedAttempt
from app.modules.reviews.application.prepare_policy_snapshot import PreparePolicySnapshot
from app.modules.reviews.application.process_run import RunConventionsInput, RunVcsInput
from app.modules.reviews.application.prompt_builder import PullRequestMeta, RepoConventions
from app.modules.reviews.application.review_context import BuildReviewContext
from app.modules.reviews.application.review_context_stage import (
    EstimatedTokenCounter,
    ReviewContextStage,
)
from app.modules.reviews.application.review_output import VerifiedReviewOutput
from app.modules.reviews.application.run_failures import RunFailure
from app.modules.reviews.application.vcs_diff import PullRequestLocator, VcsFile, VcsPullRequest
from app.modules.reviews.infrastructure.blob_cache import InMemoryBlobCache
from app.modules.reviews.infrastructure.llm.gateway import LlmGateway
from app.modules.reviews.infrastructure.llm.memory import InMemoryLlmCallTrace, InMemoryUsageLedger
from app.modules.reviews.infrastructure.llm.settings import LlmSettings
from tests.llm_fakes import context_checkpoint
from tests.policy_snapshot_fakes import make_complete_prompt_bindings
from tests.test_gateway_review_model import MAIN, SUMMARY, _finding, _profile
from tests.test_prepare_policy_snapshot import Source, Uow
from tests.test_review_context_stage import Parser, Trace
from tests.test_review_path import _PINNED, RecordingConventions, _binding, _store
from tests.test_worker_reflection_pass import _Answers, _Manifests
from tests.trigger_uow import processing_uow

RUN_ID = UUID(int=2)
REPOSITORY_ID = UUID(int=1)
ORIGIN_ID = UUID(int=169)
LOCATOR = PullRequestLocator(0, "team/project", 7, "gitlab", ORIGIN_ID, 19)
RUN = RunVcsInput(UUID(int=5), REPOSITORY_ID, "a" * 40, "b" * 40, LOCATOR)


class Runs:
    def __init__(self) -> None:
        self.snapshots: list[DiffSnapshot] | None = None

    async def get_run_vcs_input(self, run_id: UUID) -> RunVcsInput:
        assert run_id == RUN_ID
        return RUN

    async def get_run_diff_input(self, run_id: UUID) -> None:
        raise AssertionError("GitLab uses the persisted VCS locator")

    async def get_run_snapshots(self, run_id: UUID) -> list[DiffSnapshot] | None:
        return self.snapshots

    async def get_run_conventions_input(self, run_id: UUID) -> RunConventionsInput:
        return RunConventionsInput(
            REPOSITORY_ID,
            _PINNED,
            review_binding=_binding(provider="gitlab"),
            pinned_conventions_prompt=_PINNED,
        )

    async def store_diff_snapshots(
        self, run_id: UUID, code_change_id: UUID, head_sha: str, snapshots: list[DiffSnapshot]
    ) -> list[DiffSnapshot]:
        assert (run_id, code_change_id, head_sha) == (UUID(int=2), UUID(int=5), "a" * 40)
        self.snapshots = snapshots
        return snapshots

    async def get_review_prompt_input(
        self, run_id: UUID, conventions: GeneratedConventions
    ) -> ReviewPromptInput:
        assert self.snapshots is not None
        files, omitted = review_files_from_snapshots(self.snapshots)
        return ReviewPromptInput(
            "Review carefully.",
            (),
            None,
            RepoConventions((), ()),
            files,
            omitted,
            review_binding=_binding(provider="gitlab"),
        )


class GitLab:
    def __init__(self) -> None:
        self.locators: list[PullRequestLocator] = []

    async def get_pull_request(
        self, locator: PullRequestLocator, *, checkpoint: RunProviderCheckpoint
    ) -> VcsPullRequest:
        await checkpoint()
        self.locators.append(locator)
        return VcsPullRequest(
            locator,
            "a" * 40,
            "b" * 40,
            PullRequestMeta("MR", None, "author", "feature", "main", (), 1, 1, 0, False, False),
        )

    async def get_diff(
        self, pull_request: VcsPullRequest, *, checkpoint: RunProviderCheckpoint
    ) -> tuple[VcsFile, ...]:
        await checkpoint()
        return (VcsFile(MAIN, "added", "e" * 40, None, 1, 0, 1, "@@ -0,0 +1 @@\n+x = 1"),)

    async def get_blob(
        self, locator: PullRequestLocator, sha: str, *, checkpoint: RunProviderCheckpoint
    ) -> bytes:
        await checkpoint()
        return b"x = 1\n"

    async def get_merge_base(
        self, pull_request: VcsPullRequest, *, checkpoint: RunProviderCheckpoint
    ) -> str:
        await checkpoint()
        return "c" * 40

    async def get_pull_request_meta(self, run_id: UUID) -> PullRequestMeta:
        # A live read after conventions must not relabel the attempt's frozen head.
        return PullRequestMeta(
            "MR",
            None,
            "author",
            "feature",
            "main",
            (),
            1,
            1,
            0,
            False,
            False,
            head_sha="d" * 40,
        )


class Tree:
    async def tree(self, revision: str) -> RevisionTree:
        return RevisionTree(revision, {MAIN: "e" * 40}, complete=True)

    async def read_blob(self, blob_sha: str) -> bytes:
        return b"x = 1\n"


class Output:
    def __init__(self) -> None:
        self.outputs: list[object] = []

    async def execute(self, run_id: UUID, raw_output: object) -> bool:
        self.outputs.append(raw_output)
        return True


class NoGitHub:
    def __init__(self) -> None:
        self.calls = 0

    def __getattr__(self, name: str) -> Any:
        self.calls += 1
        raise AssertionError(f"GitLab must not call GitHub: {name}")


@pytest.mark.parametrize("with_verifier", [True, False], ids=["verified", "no-server-verifier"])
def test_gitlab_v4_worker_reviews_and_verifies_the_pinned_snapshot(
    monkeypatch: pytest.MonkeyPatch,
    with_verifier: bool,
) -> None:
    runs, vcs, github = Runs(), GitLab(), NoGitHub()
    store, trace, output = _store(), Trace(), Output()
    assert store.pins is not None
    store.pins = replace(
        store.pins,
        prompt_bindings=replace(
            make_complete_prompt_bindings(
                review_system_id=UUID(int=41),
                conventions_id=UUID(int=43),
                verifier_id=UUID(int=44),
            ),
            review_system_version="v4",
        ),
    )
    conventions = RecordingConventions([])
    cache = InMemoryBlobCache()
    stage = ReviewContextStage(
        runs=runs,
        checkpoint=lambda: _checkpoint(),
        vcs=vcs,
        merge_base=vcs,
        prepare_policy=PreparePolicySnapshot(
            uow_factory=lambda: Uow(store),
            source_factory=lambda pins: Source(),
        ),
        context_builder=lambda run: BuildReviewContext(
            source=Tree(),
            parser=Parser(),
            counter=EstimatedTokenCounter(),
        ),
        trace=trace,
    )
    transport = _Answers(json.dumps({"findings": [_finding(1)], "summary": SUMMARY}))
    manifests, llm_trace = _Manifests(), InMemoryLlmCallTrace()
    gateway = LlmGateway(
        LlmSettings(primary=_profile(), fallback=None),
        transport,
        InMemoryUsageLedger(),
        llm_trace,
        checkpoint=context_checkpoint,
        manifest_store=manifests,
    )
    claimed = ClaimedAttempt(
        RUN_ID,
        UUID(int=170),
        2,
        "fast",
        datetime.now(UTC) + timedelta(minutes=5),
        UUID(int=41),
        UUID(int=4),
        "gitlab-worker",
    )
    # Production attempt composition: the same pass and run context reach ExecuteReviewRun.
    provider = worker.attempt_model_provider(gateway, cast(Any, vcs), claimed)
    reflection = provider.reflection_pass
    assert reflection is not None
    monkeypatch.setattr(worker, "SqlAlchemyRunRepository", lambda *a, **kw: runs)
    monkeypatch.setattr(worker, "SqlAlchemyReviewPromptRepository", lambda *a: runs)
    monkeypatch.setattr(worker, "SqlAlchemyBlobCache", lambda *a: cache)
    monkeypatch.setattr(worker, "GenerateRepoConventions", lambda *a: conventions)
    monkeypatch.setattr(
        worker,
        "SqlAlchemyRunProcessingUnitOfWork",
        lambda *a: processing_uow(runs, cache)(),
    )

    async def process() -> bool:
        return await worker.process_review_run(
            RUN_ID,
            provider,
            cast(async_sessionmaker[AsyncSession], object()),
            cast(Any, github),
            output=output,
            context_stage=stage,
            reflection_pass=reflection if with_verifier else None,
            vcs_provider_selector=worker.BoundVcsProviderSelector(LOCATOR, vcs),
        )

    if not with_verifier:
        with pytest.raises(RunFailure) as failure:
            asyncio.run(process())
        assert failure.value.error_code == "internal_error"
        assert transport.requests == [] and output.outputs == []
        assert github.calls == 0
        return

    assert asyncio.run(process()) is True

    assert github.calls == 0
    assert vcs.locators and all(
        locator
        == PullRequestLocator(
            0,
            "team/project",
            7,
            "gitlab",
            UUID(int=169),
            19,
        )
        for locator in vcs.locators
    )
    assert stage.head_sha == "a" * 40 and stage.diff_old_sha == "c" * 40
    assert stage.snapshot_for_llm is store.snapshot
    assert store.snapshot is not None and store.snapshot.policy_base_sha == "b" * 40
    assert store.snapshot.prompt_bindings.review_system_id == UUID(int=41)
    assert store.snapshot.prompt_bindings.conventions_id == UUID(int=43)
    assert store.snapshot.prompt_bindings.verifier_id == UUID(int=44)
    assert conventions.calls[0]["snapshot"] is store.snapshot
    assert conventions.calls[0]["conventions_prompt"] == _PINNED
    assert [entry.manifest.stage for entry in manifests.entries] == ["review", "verification"]
    assert [record.operation for _, record in llm_trace.records] == ["review", "verification"]
    review_manifest, verify_manifest = manifests.entries
    changed = next(block for block in review_manifest.manifest.blocks if block.path == MAIN)
    assert changed.revision == "a" * 40
    assert review_manifest.manifest.run_id == RUN_ID
    (verified,) = output.outputs
    assert isinstance(verified, VerifiedReviewOutput)
    assert verified.verify_request is not None
    assert verified.verify_request["manifest_id"] == str(verify_manifest.manifest_id)
    findings = cast(list[Mapping[str, object]], verified["findings"])
    assert [(item["path"], item["line"]) for item in findings] == [("app/main.py", 1)]
    assert len(transport.requests) == 2


async def _checkpoint() -> None:
    pass
