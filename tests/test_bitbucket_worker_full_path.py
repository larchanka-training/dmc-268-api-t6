"""A Bitbucket run reaches the ordinary review and verifier without other VCS ports."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import UUID

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import app.worker as worker
from app.modules.reviews.application.handle_review_run import ClaimedAttempt
from app.modules.reviews.application.policy_errors import PolicyError
from app.modules.reviews.application.prepare_policy_snapshot import PreparePolicySnapshot
from app.modules.reviews.application.process_run import RunVcsInput
from app.modules.reviews.application.review_context import BuildReviewContext
from app.modules.reviews.application.review_context_stage import (
    EstimatedTokenCounter,
    ReviewContextStage,
)
from app.modules.reviews.application.review_path import ReviewBinding, require_served_provider
from app.modules.reviews.application.run_failures import RunFailure
from app.modules.reviews.application.vcs_diff import PullRequestLocator
from app.modules.reviews.infrastructure.blob_cache import InMemoryBlobCache
from app.modules.reviews.infrastructure.llm.gateway import LlmGateway
from app.modules.reviews.infrastructure.llm.memory import InMemoryLlmCallTrace, InMemoryUsageLedger
from app.modules.reviews.infrastructure.llm.settings import LlmSettings
from tests.llm_fakes import context_checkpoint
from tests.policy_snapshot_fakes import make_complete_prompt_bindings
from tests.test_bitbucket_vcs import DEST
from tests.test_gateway_review_model import SUMMARY, _finding, _profile
from tests.test_gitlab_snapshot_path import GitLab, NoGitHub, Output, Runs, Tree, _checkpoint
from tests.test_prepare_policy_snapshot import Source, Uow
from tests.test_review_context_stage import Parser, Trace
from tests.test_review_path import RecordingConventions, _binding, _store
from tests.test_worker_reflection_pass import _Answers, _Manifests
from tests.trigger_uow import processing_uow

RUN_ID = UUID(int=2)
REPO_ID = UUID(int=1)
LOCATOR = PullRequestLocator(0, "team/project", 7, "bitbucket", UUID(int=169), 19, DEST)
RUN = RunVcsInput(UUID(int=5), REPO_ID, "a" * 40, "b" * 40, LOCATOR)


@pytest.mark.parametrize("provider", ["github", "gitlab", "bitbucket"])
def test_snapshot_review_provider_gate_serves_known_providers(provider: str) -> None:
    require_served_provider(ReviewBinding(UUID(int=1), "review.system", "v4", provider))
    with pytest.raises(PolicyError):
        require_served_provider(ReviewBinding(UUID(int=1), "review.system", "v4", "unknown"))


class BitbucketRuns(Runs):
    async def get_run_vcs_input(self, run_id: UUID) -> RunVcsInput:
        assert run_id == RUN_ID
        return RUN

    async def get_run_conventions_input(self, run_id: UUID):  # type: ignore[no-untyped-def]
        return replace(
            await super().get_run_conventions_input(run_id),
            review_binding=_binding(provider="bitbucket"),
        )

    async def get_review_prompt_input(self, run_id: UUID, conventions):  # type: ignore[no-untyped-def]
        return replace(
            await super().get_review_prompt_input(run_id, conventions),
            review_binding=_binding(provider="bitbucket"),
        )


@pytest.mark.parametrize("with_verifier", [True, False])
def test_bitbucket_full_mocked_review_never_uses_github_or_gitlab(
    monkeypatch: pytest.MonkeyPatch, with_verifier: bool
) -> None:
    runs, vcs, forbidden = BitbucketRuns(), GitLab(), NoGitHub()
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
    conventions, cache = RecordingConventions([]), InMemoryBlobCache()
    stage = ReviewContextStage(
        runs=runs,
        checkpoint=_checkpoint,
        vcs=vcs,
        merge_base=vcs,
        prepare_policy=PreparePolicySnapshot(
            uow_factory=lambda: Uow(store), source_factory=lambda pins: Source()
        ),
        context_builder=lambda run: BuildReviewContext(
            source=Tree(), parser=Parser(), counter=EstimatedTokenCounter()
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
        "bitbucket-worker",
    )
    provider = worker.attempt_model_provider(gateway, cast(Any, vcs), claimed)
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
            cast(Any, forbidden),
            output=output,
            context_stage=stage,
            reflection_pass=provider.reflection_pass if with_verifier else None,
            vcs_provider_selector=worker.BoundVcsProviderSelector(LOCATOR, vcs),
        )

    if not with_verifier:
        with pytest.raises(RunFailure):
            asyncio.run(process())
        assert output.outputs == []
    else:
        assert asyncio.run(process()) is True
        assert len(output.outputs) == 1
        assert stage.head_sha == RUN.head_sha
        assert stage.snapshot_for_llm is store.snapshot
    assert forbidden.calls == 0
