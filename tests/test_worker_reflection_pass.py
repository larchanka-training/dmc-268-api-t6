"""The worker runs the Reflection Pass on snapshot-path reviews (#130 F1-7, R14).

``attempt_provider_factory`` builds one ``ReflectionPass(GatewayVerifierModel(gateway, run))``
per attempt; the queue pipeline hands it to ``process_review_run``, which gives it to
``ExecuteReviewRun``. A legacy run never calls it (``tests/test_snapshot_path_live_pin.py``
pins that); a run bound to a v2-contract review prompt makes the verification call, and its
``llm.verify`` names the stored manifest of the accepted verifier call. No network: provider
answers come from an in-memory transport.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Callable
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import app.modules.reviews.application.reflection_pass as reflection_pass_module
import app.worker as worker
from app.modules.reviews.application.check_runs import RunProviderCheckpoint
from app.modules.reviews.application.execute_review import ExecuteReviewRun
from app.modules.reviews.application.handle_review_run import ClaimedAttempt
from app.modules.reviews.application.llm import ManifestEntry
from app.modules.reviews.application.process_run import RunVcsInput
from app.modules.reviews.application.prompt_builder import file_block_id
from app.modules.reviews.application.reflection_pass import ReflectionPass
from app.modules.reviews.application.review_output import VerifiedReviewOutput
from app.modules.reviews.application.vcs_diff import PullRequestLocator
from app.modules.reviews.infrastructure.github_run_source import GitHubRunSource
from app.modules.reviews.infrastructure.llm.gateway import LlmGateway
from app.modules.reviews.infrastructure.llm.memory import InMemoryLlmCallTrace, InMemoryUsageLedger
from app.modules.reviews.infrastructure.llm.models import GatewayVerifierModel
from app.modules.reviews.infrastructure.llm.settings import LlmSettings
from app.modules.reviews.infrastructure.llm.transport import ChatRequest, ChatResponse
from tests.llm_fakes import context_checkpoint
from tests.test_gateway_review_model import (
    MAIN,
    RUN,
    RUN_ID,
    SUMMARY,
    _finding,
    _GatewayModel,
    _one_line,
    _Pipeline,
    _profile,
    _snapshot,
)

DEADLINE = datetime(2026, 10, 25, 12, 0, tzinfo=UTC)
CLAIMED = ClaimedAttempt(
    run_id=UUID("00000000-0000-0000-0000-000000017001"),
    workspace_id=UUID("00000000-0000-0000-0000-000000017002"),
    attempt=2,
    engine="fast",
    deadline=DEADLINE,
    prompt_version_id=UUID("00000000-0000-0000-0000-000000017003"),
    rule_version_id=UUID("00000000-0000-0000-0000-000000017004"),
    worker_id="worker-1",
)


def _github(run_source: Callable[[UUID], GitHubRunSource] | None = None) -> worker.GitHubAdapters:
    return worker.GitHubAdapters(
        vcs=cast(Any, object()),
        check_runs=cast(Any, object()),
        reviews=cast(Any, object()),
        eligibility=cast(Any, object()),
        run_source=run_source or (lambda run_id: cast(GitHubRunSource, object())),
    )


# ---------- the composition ----------


def test_the_production_factory_gives_each_attempt_a_reflection_pass_bound_to_it() -> None:
    # Building the pass never calls the gateway: the window is read when the verifier runs.
    gateway = cast(LlmGateway, object())

    provider = worker.attempt_provider_factory(gateway, _github())(CLAIMED)

    assert isinstance(provider, worker.AttemptReviewProvider)
    reflection = provider.reflection_pass
    assert isinstance(reflection, ReflectionPass)
    verifier = reflection._model
    assert isinstance(verifier, GatewayVerifierModel)
    assert verifier._gateway is gateway
    assert verifier._run.run_id == CLAIMED.run_id
    assert verifier._run.attempt == 2
    assert verifier._run.deadline == DEADLINE
    assert verifier._run.engine == "fast"


class _NoPromptFiles:
    """``importlib.resources.files`` of a package whose prompt files are gone."""

    def joinpath(self, name: str) -> _NoPromptFiles:
        return self

    def read_text(self, encoding: str) -> str:
        raise FileNotFoundError("review.verifier.v2.md")


def test_building_an_attempt_reads_no_verifier_prompt_file(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Every attempt gets a pass, a legacy one too, which never verifies: a missing verifier
    # prompt file must not fail it. The pass reads the file when it first verifies.
    monkeypatch.setattr(reflection_pass_module, "files", lambda package: _NoPromptFiles())

    provider = worker.attempt_provider_factory(cast(LlmGateway, object()), _github())(CLAIMED)

    assert isinstance(provider, worker.AttemptReviewProvider)
    assert isinstance(provider.reflection_pass, ReflectionPass)


def test_a_worker_without_llm_settings_builds_no_reflection_pass() -> None:
    provider = worker.attempt_provider_factory(None, _github())(CLAIMED)

    assert isinstance(provider, worker.AttemptReviewProvider)
    assert provider.reflection_pass is None


def _settings() -> worker.WorkerSettings:
    return worker.WorkerSettings(
        database_url="unused",
        rabbitmq_url="unused",
        worker_id="worker-1",
        github_app_id="1",
        github_private_key="unused",
        github_api_url="https://api.github.test",
        portal_url=None,
    )


class _NoStore:
    def __init__(self, *args: object, **kwargs: object) -> None:
        pass


def _pipeline_kwargs(monkeypatch: pytest.MonkeyPatch, **compose: Any) -> dict[str, Any]:
    captured: dict[str, Any] = {}

    async def record(*args: object, **kwargs: Any) -> bool:
        captured.update(kwargs)
        return True

    class Runs:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        async def get_run_vcs_input(self, run_id: UUID) -> RunVcsInput:
            assert run_id == CLAIMED.run_id
            return RunVcsInput(
                UUID(int=4),
                UUID(int=5),
                "a" * 40,
                "b" * 40,
                PullRequestLocator(1, "team/repo", 7, "github", UUID(int=6), 1),
            )

    monkeypatch.setattr(worker, "SqlAlchemyRunRepository", Runs)
    monkeypatch.setattr(worker, "StoreReviewOutput", _NoStore)
    monkeypatch.setattr(worker, "process_review_run", record)
    process = worker.compose_worker_process(
        settings=_settings(),
        session_factory=cast(async_sessionmaker[AsyncSession], object()),
        queue=cast(Any, object()),
        github=_github(),
        **compose,
    )
    assert asyncio.run(process.handle_run._pipeline(CLAIMED)) is True
    return captured


def test_the_queue_pipeline_hands_the_attempt_reflection_pass_to_the_review(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gateway = cast(LlmGateway, object())

    kwargs = _pipeline_kwargs(monkeypatch, gateway=gateway)

    reflection = kwargs["reflection_pass"]
    assert isinstance(reflection, ReflectionPass)
    assert isinstance(reflection._model, GatewayVerifierModel)
    assert reflection._model._gateway is gateway
    assert reflection._model._run.attempt == CLAIMED.attempt


def test_an_injected_provider_runs_without_a_reflection_pass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kwargs = _pipeline_kwargs(
        monkeypatch,
        gateway=cast(LlmGateway, object()),
        provider_factory=lambda claimed: cast(Any, object()),
    )

    assert kwargs["reflection_pass"] is None


@pytest.mark.parametrize("with_stage", [False, True], ids=["no-stage", "stage"])
def test_process_review_run_gives_the_reflection_pass_to_the_review(
    monkeypatch: pytest.MonkeyPatch, with_stage: bool
) -> None:
    captured: list[dict[str, Any]] = []

    class RecordingRun:
        def __init__(self, *args: object, **kwargs: Any) -> None:
            captured.append(kwargs)

        async def execute(self, run_id: UUID) -> bool:
            return True

    monkeypatch.setattr(worker, "ExecuteReviewRun", RecordingRun)
    reflection = ReflectionPass(cast(Any, object()))
    stage = SimpleNamespace(excluded=lambda path: False) if with_stage else None

    assert asyncio.run(
        worker.process_review_run(
            CLAIMED.run_id,
            cast(Any, object()),
            cast(async_sessionmaker[AsyncSession], object()),
            cast(Any, object()),
            context_stage=cast(Any, stage),
            reflection_pass=reflection,
        )
    )

    assert len(captured) == 1
    assert captured[0]["reflection_pass"] is reflection


# ---------- a v4-bound review through the gateway ----------


class _Answers:
    """The provider: the review answer first, then a verifier answer for the shown ids."""

    def __init__(self, review: str) -> None:
        self.review = review
        self.requests: list[ChatRequest] = []

    async def complete(
        self, request: ChatRequest, *, checkpoint: RunProviderCheckpoint
    ) -> ChatResponse:
        await checkpoint()
        self.requests.append(request)
        if len(self.requests) == 1:
            content = self.review
        elif len(self.requests) == 2:
            ids = re.findall(r'candidate_id="([^"]+)"', request.messages[-1].content)
            content = json.dumps(
                {
                    "decisions": [
                        {
                            "candidate_id": candidate,
                            "status": "supported",
                            "reason": "The added line shows it.",
                            "evidence_refs": [
                                {"block_id": file_block_id(MAIN), "start_line": 1, "end_line": 1}
                            ],
                            "requirement_refs": [],
                        }
                        for candidate in ids
                    ]
                }
            )
        else:
            raise AssertionError("unexpected provider call")
        return ChatResponse(
            raw={"choices": [{"message": {"content": content}}]},
            content=content,
            finish_reason="stop",
            model=request.profile.model,
            prompt_tokens=10,
            completion_tokens=20,
            cached_tokens=0,
            cost=None,
            cost_currency=None,
        )


class _Manifests:
    def __init__(self) -> None:
        self.entries: list[ManifestEntry] = []

    async def record_manifest(self, run_id: UUID, entry: ManifestEntry) -> None:
        self.entries.append(entry)


def test_a_v4_review_makes_a_verification_call_and_llm_verify_names_its_manifest() -> None:
    transport = _Answers(json.dumps({"findings": [_finding(1)], "summary": SUMMARY}))
    trace = InMemoryLlmCallTrace()
    manifests = _Manifests()
    gateway = LlmGateway(
        LlmSettings(primary=_profile(), fallback=None),
        transport,
        InMemoryUsageLedger(),
        trace,
        checkpoint=context_checkpoint,
        manifest_store=manifests,
    )
    pipeline = _Pipeline(_one_line(), _snapshot())
    review = ExecuteReviewRun(
        pipeline,
        pipeline,
        _GatewayModel(gateway, pipeline),  # type: ignore[arg-type]
        pipeline,
        reflection_pass=ReflectionPass(GatewayVerifierModel(gateway, RUN)),
    )

    assert asyncio.run(review.execute(RUN_ID)) is True

    assert [entry.manifest.stage for entry in manifests.entries] == ["review", "verification"]
    verification = manifests.entries[1]
    records = [record for _, record in trace.records]
    assert [(record.operation, record.stage) for record in records] == [
        ("review", "review"),
        ("verification", "verification"),
    ]
    assert records[1].manifest_id == verification.manifest_id
    assert records[1].call_id == verification.manifest.call_id
    (output,) = pipeline.published
    assert isinstance(output, VerifiedReviewOutput)
    assert output.verify_request is not None
    assert output.verify_request["manifest_id"] == str(verification.manifest_id)
    assert output.verify_request["accepted_call_id"] == str(verification.manifest.call_id)
    findings = cast(list[dict[str, Any]], output["findings"])
    assert [finding["line"] for finding in findings] == [1]
