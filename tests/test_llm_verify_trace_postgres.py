"""``enter_publishing`` writes the ``llm.verify`` run action in PostgreSQL (#130 PR E, R13).

The row sits before ``review.postprocess`` in the publishing transaction, at most once per
run, and only when the store passes a verifier trace: a legacy run writes none.
"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Iterator, Mapping
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from functools import partial
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.common.infrastructure.db.enums import RunState
from app.modules.reviews.application.execute_review import (
    ExecuteReviewRun,
    ReviewPromptInput,
    ReviewReply,
)
from app.modules.reviews.application.findings_post_processor import ProcessedReviewOutput
from app.modules.reviews.application.llm import ManifestEntry, TransactionalManifestStore
from app.modules.reviews.application.policy_snapshot import CallManifest
from app.modules.reviews.application.prompt_builder import (
    ChangedFile,
    DiffLine,
    PromptBuilder,
    PullRequestMeta,
    RepoConventions,
    ReviewContext,
    file_block_id,
)
from app.modules.reviews.application.reflection_pass import (
    ReflectionPass,
    VerificationUnavailableError,
)
from app.modules.reviews.application.review_output import (
    ReviewOutput,
    ReviewSummary,
    parse_review_output_v2,
)
from app.modules.reviews.application.run_coverage import CoverageItem, CoverageStatus, RunCoverage
from app.modules.reviews.application.store_review_output import (
    AcceptedReviewCheckpoint,
    StoreReviewOutput,
)
from app.modules.reviews.infrastructure.context_manifest_store import (
    SqlAlchemyContextManifestUnitOfWork,
)
from app.modules.reviews.infrastructure.models import FinalReviewResult, Run, RunAction
from app.modules.reviews.infrastructure.review_output_unit_of_work import (
    SqlAlchemyReviewOutputUnitOfWork,
)
from app.modules.reviews.infrastructure.run_repository import SqlAlchemyReviewOutputRepository
from tests.execute_review_fakes import FakeVerifierModel, _make_test_snapshot, conventions
from tests.portal_postgres import HEAD, NOW, REPO_A, RUN_ATTEMPTED, Env, portal_schema
from tests.run_coverage_fakes import code_snapshot
from tests.test_clean_without_text_result_postgres import prepare_result
from tests.test_context_manifests_postgres import INSERT_POLICY_SNAPSHOT, policy_snapshot_values
from tests.test_run_step_postgres import allow_run_steps
from tests.test_run_stream_postgres import insert_queued_run

STARTED = datetime(2026, 10, 24, 9, 0, 0, tzinfo=UTC)
VERIFY_REQUEST: dict[str, Any] = {
    # Prompt order, not sorted.
    "candidate_ids": ["c-2", "c-1"],
    "accepted_call_id": "00000000-0000-0000-0000-000000013911",
    "manifest_id": None,
}
VERIFY_RESPONSE: dict[str, Any] = {
    "decisions": [
        {
            "candidate_id": "c-2",
            "status": "insufficient_context",
            "evidence_refs": [],
            "requirement_refs": [],
        },
        {
            "candidate_id": "c-1",
            "status": "supported",
            "evidence_refs": [{"block_id": "file-1", "start_line": 10, "end_line": 10}],
            "requirement_refs": [],
        },
    ]
}


@pytest.fixture
def env() -> Iterator[Env]:
    database_url = os.environ.get("TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("set TEST_DATABASE_URL to run PostgreSQL integration tests")
    with portal_schema(database_url, None) as schema:
        yield schema


def _model_output() -> dict[str, object]:
    return {
        "findings": [],
        "summary": {"problem": "A bug.", "done_well": "Small change.", "effort": "small"},
    }


def _parsed_output() -> ReviewOutput:
    return ReviewOutput(
        findings=[],
        summary=ReviewSummary(problem="A bug.", done_well="Small change.", effort="small"),
    )


def _processed_output() -> ProcessedReviewOutput:
    return ProcessedReviewOutput(
        inline=(),
        body_only=(),
        dropped=(),
        review_body="A bug.",
        coverage=RunCoverage(
            CoverageStatus.PARTIAL,
            0,
            reasons=(CoverageItem(None, "classification_unavailable"),),
        ),
    )


async def _run_ready_for_actions(factory: async_sessionmaker[AsyncSession]) -> UUID:
    """Publishing writes run actions, which require ``runs.attempt >= 1`` (#170)."""
    run_id = await insert_queued_run(factory)
    await allow_run_steps(factory, run_id)
    return run_id


async def _publish(
    factory: async_sessionmaker[AsyncSession],
    run_id: UUID,
    *,
    trace: bool,
    commit: bool = True,
) -> None:
    async with SqlAlchemyReviewOutputUnitOfWork(factory) as uow:
        stored = await uow.reviews.store_review_output(
            run_id, _model_output(), _parsed_output(), _processed_output()
        )
        assert stored is not None
        await uow.reviews.enter_publishing(
            run_id,
            lease_until=NOW + timedelta(minutes=5),
            postprocess_request={"min_confidence": 0.5, "max_inline": 8},
            postprocess_response={"inline": 0, "body_only": 0, "dropped": []},
            started_at=STARTED,
            duration_ms=11,
            verify_request=VERIFY_REQUEST if trace else None,
            verify_response=VERIFY_RESPONSE if trace else None,
        )
        if commit:
            await uow.commit()
        else:
            await uow.rollback()


async def _actions(
    factory: async_sessionmaker[AsyncSession], run_id: UUID
) -> list[tuple[int, str, Any, Any]]:
    async with factory() as session:
        rows = await session.execute(
            select(RunAction.index, RunAction.tool, RunAction.request, RunAction.response)
            .where(RunAction.run_id == run_id)
            .order_by(RunAction.index)
        )
        return [(index, tool, request, response) for index, tool, request, response in rows]


async def _timings(
    factory: async_sessionmaker[AsyncSession], run_id: UUID
) -> list[tuple[str, datetime, int]]:
    async with factory() as session:
        rows = await session.execute(
            select(RunAction.tool, RunAction.started_at, RunAction.duration_ms)
            .where(RunAction.run_id == run_id)
            .order_by(RunAction.index)
        )
        return [(tool, started_at, duration_ms) for tool, started_at, duration_ms in rows]


@pytest.mark.integration
def test_candidate_checkpoint_commits_in_running_and_reuses_across_attempts(env: Env) -> None:
    engine = env.engine()
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async def scenario() -> tuple[str, list[tuple[int, str, Any, Any]]]:
        try:
            run_id = await _run_ready_for_actions(factory)
            async with factory() as session, session.begin():
                run = await session.get(Run, run_id)
                assert run is not None
                run.state = RunState.RUNNING
                run.worker_id = "worker"

            class Queue:
                async def publish_review(self, pointer: Any) -> None:
                    raise AssertionError("candidate checkpoint must not publish")

            store = StoreReviewOutput(
                lambda: SqlAlchemyReviewOutputUnitOfWork(factory),
                Queue(),
                worker_id="worker",
                expected_attempt=1,
                run_url=lambda _: None,
            )
            assert await store.checkpoint_candidates(
                run_id,
                _model_output(),
                ("candidate-1",),
                candidates=[{"title": "Accepted candidate"}],
                dropped=[],
                accepted_call_id=UUID(int=435),
                incomplete_paths=(),
                fitted_context={"test": "candidate"},
            )
            with pytest.raises(ValueError, match="changed within attempt"):
                await store.checkpoint_candidates(
                    run_id,
                    _model_output(),
                    ("candidate-1",),
                    candidates=[{"title": "Changed candidate"}],
                    dropped=[],
                    accepted_call_id=UUID(int=435),
                    incomplete_paths=(),
                    fitted_context={"test": "candidate"},
                )
            async with factory() as session, session.begin():
                run = await session.get(Run, run_id)
                assert run is not None
                run.attempt = 2
            # Same worker id is not a claim token: the previous invocation is stale.
            assert not await store.checkpoint_candidates(
                run_id,
                _model_output(),
                ("candidate-1",),
                candidates=[{"title": "Accepted candidate"}],
                dropped=[],
                accepted_call_id=UUID(int=435),
                incomplete_paths=(),
                fitted_context={"test": "candidate"},
            )
            retry_store = StoreReviewOutput(
                lambda: SqlAlchemyReviewOutputUnitOfWork(factory),
                Queue(),
                worker_id="worker",
                expected_attempt=2,
                run_url=lambda _: None,
            )
            assert await retry_store.checkpoint_candidates(
                run_id,
                _model_output(),
                ("candidate-1",),
                candidates=[{"title": "Accepted candidate"}],
                dropped=[],
                accepted_call_id=UUID(int=435),
                incomplete_paths=(),
                fitted_context={"test": "candidate"},
            )
            saved = await retry_store.load_accepted_output(run_id)
            assert saved is not None
            assert saved.output == _model_output()
            assert saved.source_attempt == 1
            async with factory() as session:
                state = await session.scalar(select(Run.state).where(Run.id == run_id))
            assert state is not None
            return state.value, await _actions(factory, run_id)
        finally:
            await engine.dispose()

    state, actions = asyncio.run(scenario())
    assert state == "running"
    assert [(index, tool) for index, tool, _, _ in actions] == [
        (0, "llm.review_output"),
        (1, "review.postprocess"),
    ]
    assert actions[0][2]["attempt"] == 1
    assert actions[0][2]["accepted_call_id"] == str(UUID(int=435))
    assert UUID(actions[0][2]["checkpoint_id"])
    assert len(actions[0][2]["digest"]) == 64
    assert actions[0][3] == _model_output()
    assert actions[1][2] == {
        "attempt": 1,
        "checkpoint_id": actions[0][2]["checkpoint_id"],
        "digest": actions[0][2]["digest"],
        "phase": "candidate",
    }
    assert actions[1][3] == {
        "candidate_ids": ["candidate-1"],
        "dropped": [],
    }


@pytest.mark.integration
def test_large_accepted_checkpoint_reloads_without_exposing_fitted_diff(env: Env) -> None:
    engine = env.engine()
    factory = async_sessionmaker(engine, expire_on_commit=False)
    private_line = "private diff line must remain outside run_actions.request"
    output: dict[str, object] = {
        "findings": [],
        "summary": {
            "problem": "A" * 1_100_000 + ".",
            "done_well": "The change is small.",
            "effort": "none",
        },
    }
    assert parse_review_output_v2(output).summary.problem == output["summary"]["problem"]  # type: ignore[index]

    async def scenario() -> tuple[
        AcceptedReviewCheckpoint | None, list[dict[str, Any]], str | None
    ]:
        try:
            run_id = await _run_ready_for_actions(factory)
            async with factory() as session, session.begin():
                run = await session.get(Run, run_id)
                assert run is not None
                run.state = RunState.RUNNING
                run.worker_id = "worker"

            class Queue:
                async def publish_review(self, pointer: Any) -> None:
                    raise AssertionError("candidate checkpoint must not publish")

            store = StoreReviewOutput(
                lambda: SqlAlchemyReviewOutputUnitOfWork(factory),
                Queue(),
                worker_id="worker",
                expected_attempt=1,
                run_url=lambda _: None,
            )
            assert await store.checkpoint_candidates(
                run_id,
                output,
                (),
                candidates=[],
                dropped=[],
                accepted_call_id=None,
                incomplete_paths=(),
                fitted_context={"changed_files": [{"lines": [[1, "added", private_line]]}]},
            )
            saved = await store.load_accepted_output(run_id)
            async with factory() as session:
                actions = (
                    (
                        await session.execute(
                            select(RunAction)
                            .where(RunAction.run_id == run_id)
                            .order_by(RunAction.index)
                        )
                    )
                    .scalars()
                    .all()
                )
            assert [action.tool for action in actions] == [
                "llm.review_output",
                "review.postprocess",
            ]
            return saved, [action.request for action in actions], actions[0].response_ref
        finally:
            await engine.dispose()

    saved, requests, response_ref = asyncio.run(scenario())
    assert saved is not None and saved.output == output
    assert saved.fitted_context == {"changed_files": [{"lines": [[1, "added", private_line]]}]}
    assert all(private_line not in json.dumps(request) for request in requests)
    assert response_ref is not None  # Diagnostic response remains subject to the 1 MiB cap.


@pytest.mark.integration
def test_execute_review_retry_reloads_committed_candidate_checkpoint(env: Env) -> None:
    engine = env.engine()
    factory = async_sessionmaker(engine, expire_on_commit=False)
    reviewer_calls = 0
    verifier_calls: list[tuple[str, ...]] = []
    accepted_call_id = UUID(int=435)
    accepted_manifest: CallManifest | None = None

    class Processor:
        async def prepare(self, run_id: UUID) -> Any:
            return conventions()

    class Prompts:
        async def get_review_prompt_input(self, run_id: UUID, saved: Any) -> ReviewPromptInput:
            return ReviewPromptInput(
                system="Review carefully.",
                rules=(),
                agents_md=None,
                conventions=RepoConventions((), ()),
                changed_files=(
                    ChangedFile("app/main.py", "modified", (DiffLine(10, "added", "query = ..."),)),
                    ChangedFile("app/omitted.py", "modified", (DiffLine(3, "added", "x = 1"),)),
                ),
                omitted_files=(),
                policy_snapshot=replace(_make_test_snapshot(), run_id=run_id, repository_id=REPO_A),
            )

    class Model:
        async def get_pull_request_meta(self, run_id: UUID) -> PullRequestMeta:
            return PullRequestMeta("PR", None, "u", "b", "m", (), 2, 2, 0, False, False)

        async def draft_review(self, *, context: ReviewContext) -> ReviewReply:
            nonlocal reviewer_calls, accepted_manifest
            reviewer_calls += 1
            assert reviewer_calls == 1
            fitted = replace(
                context,
                changed_files=context.changed_files[:1],
                omitted_files=("app/omitted.py",),
            )
            assert context.policy_snapshot is not None
            accepted_manifest = PromptBuilder().build_manifest(
                fitted, call_id=accepted_call_id, stage="review", recovery_kind="repair"
            )
            await TransactionalManifestStore(
                partial(SqlAlchemyContextManifestUnitOfWork, factory)
            ).record_manifest(
                context.policy_snapshot.run_id,
                ManifestEntry(
                    manifest_id=UUID(int=436),
                    attempt=1,
                    policy_snapshot_id=context.policy_snapshot.snapshot_id,
                    manifest=accepted_manifest,
                    parent_call_id=UUID(int=434),
                    input_tokens=40,
                    started_at=STARTED,
                    rendered_messages_sha256="1" * 64,
                    prompt_checksum="2" * 64,
                    model_profile_version="3" * 64,
                ),
            )
            return ReviewReply(
                {
                    "findings": [
                        {
                            "path": "app/main.py",
                            "line": 10,
                            "start_line": None,
                            "severity": "high",
                            "category": "security",
                            "title": "SQL injection",
                            "body": "Raw query concatenation",
                            "suggestion": None,
                            "confidence": 0.9,
                            "rule_name": None,
                            "evidence_refs": [
                                {
                                    "block_id": file_block_id("app/main.py"),
                                    "start_line": 10,
                                    "end_line": 10,
                                }
                            ],
                        }
                    ],
                    "summary": {
                        "problem": "A bug.",
                        "done_well": "Small change.",
                        "effort": "small",
                    },
                },
                fitted,
                accepted_manifest,
            )

    class Verifier:
        async def verify(
            self, *, system_prompt: str, user_prompt: str, **kwargs: object
        ) -> Mapping[str, Any]:
            context = kwargs["context"]
            assert isinstance(context, ReviewContext)
            assert [file.path for file in context.changed_files] == ["app/main.py"]
            assert context.omitted_files == ("app/omitted.py",)
            assert "SQL injection" in user_prompt
            verifier_calls.append(context.omitted_files)
            raise VerificationUnavailableError("verifier unavailable")

    class Queue:
        async def publish_review(self, pointer: Any) -> None:
            raise AssertionError("a failed verifier cannot publish")

    async def scenario() -> tuple[list[tuple[int, str, Any, Any]], AcceptedReviewCheckpoint]:
        try:
            run_id = await _run_ready_for_actions(factory)
            async with factory.begin() as session:
                await session.execute(
                    text(INSERT_POLICY_SNAPSHOT),
                    policy_snapshot_values(run_id, _make_test_snapshot().snapshot_id),
                )
                run = await session.get(Run, run_id)
                assert run is not None
                run.state = RunState.RUNNING
                run.worker_id = "worker"
            for attempt in (1, 2):
                pipeline = ExecuteReviewRun(
                    Processor(),
                    Prompts(),
                    Model(),
                    StoreReviewOutput(
                        lambda: SqlAlchemyReviewOutputUnitOfWork(factory),
                        Queue(),
                        worker_id="worker",
                        expected_attempt=attempt,
                        run_url=lambda _: None,
                    ),
                    reflection_pass=ReflectionPass(Verifier()),
                )
                with pytest.raises(VerificationUnavailableError, match="verifier unavailable"):
                    await pipeline.execute(run_id)
                if attempt == 1:
                    async with factory.begin() as session:
                        run = await session.get(Run, run_id)
                        assert run is not None and run.state is RunState.RUNNING
                        run.attempt = 2
            store = StoreReviewOutput(
                lambda: SqlAlchemyReviewOutputUnitOfWork(factory),
                Queue(),
                worker_id="worker",
                expected_attempt=2,
                run_url=lambda _: None,
            )
            saved = await store.load_accepted_output(run_id)
            assert saved is not None
            return await _actions(factory, run_id), saved
        finally:
            await engine.dispose()

    actions, saved = asyncio.run(scenario())
    assert reviewer_calls == 1
    assert len(verifier_calls) == 2
    assert saved.source_attempt == 1
    assert saved.accepted_call_id == accepted_call_id
    assert saved.manifest == accepted_manifest
    assert saved.manifest is not None and saved.manifest.recovery_kind == "repair"
    assert saved.manifest.get_block(file_block_id("app/main.py")) is not None
    assert saved.fitted_context is not None
    assert [(index, tool) for index, tool, _, _ in actions] == [
        (0, "context.manifest"),
        (1, "llm.review_output"),
        (2, "review.postprocess"),
    ]


@pytest.mark.integration
def test_retry_after_final_commit_skips_reviewer_and_verifier(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = env.engine()
    factory = async_sessionmaker(engine, expire_on_commit=False)
    reviewer_calls = 0
    verifier_calls = 0

    class Processor:
        async def prepare(self, run_id: UUID) -> Any:
            return conventions()

    class Prompts:
        async def get_review_prompt_input(self, run_id: UUID, saved: Any) -> ReviewPromptInput:
            return ReviewPromptInput(
                system="Review carefully.",
                rules=(),
                agents_md=None,
                conventions=RepoConventions((), ()),
                changed_files=(
                    ChangedFile("src/app.py", "modified", (DiffLine(1, "added", "new"),)),
                ),
                omitted_files=(),
                policy_snapshot=_make_test_snapshot(),
            )

    class Model:
        async def get_pull_request_meta(self, run_id: UUID) -> PullRequestMeta:
            return PullRequestMeta(
                "PR", None, "u", "b", "m", (), 1, 1, 1, False, False, head_sha=HEAD
            )

        async def draft_review(self, *, context: ReviewContext) -> ReviewReply:
            nonlocal reviewer_calls
            reviewer_calls += 1
            assert reviewer_calls == 1
            output = {
                "findings": [
                    {
                        "path": "src/app.py",
                        "line": 1,
                        "start_line": None,
                        "severity": "high",
                        "category": "security",
                        "title": "Unchecked authorization",
                        "body": "A remote caller can bypass the permission check.",
                        "suggestion": None,
                        "confidence": 0.9,
                        "rule_name": None,
                        "evidence_refs": [
                            {
                                "block_id": file_block_id("src/app.py"),
                                "start_line": 1,
                                "end_line": 1,
                            }
                        ],
                    }
                ],
                "summary": {"problem": "A bug.", "done_well": "Small change.", "effort": "small"},
            }
            return ReviewReply(
                output,
                context,
                PromptBuilder().build_manifest(context, call_id=UUID(int=435), stage="review"),
            )

    class Verifier(FakeVerifierModel):
        async def verify(
            self, *, system_prompt: str, user_prompt: str, **kwargs: object
        ) -> Mapping[str, Any]:
            nonlocal verifier_calls
            verifier_calls += 1
            if verifier_calls > 1:
                raise VerificationUnavailableError("verifier unavailable on retry")
            return await super().verify(
                system_prompt=system_prompt, user_prompt=user_prompt, **kwargs
            )

    verifier = Verifier(
        {
            "decisions": [
                {
                    "candidate_id": "candidate",
                    "status": "supported",
                    "reason": "Shown on the diff.",
                    "evidence_refs": [
                        {"block_id": file_block_id("src/app.py"), "start_line": 1, "end_line": 1}
                    ],
                    "requirement_refs": [],
                }
            ]
        }
    )

    class Queue:
        def __init__(self) -> None:
            self.pointers: list[Any] = []
            self.committed_markers: list[int] = []

        async def publish_review(self, pointer: Any) -> None:
            async with factory() as session:
                run = await session.get(Run, pointer.run_id)
                markers = (
                    await session.scalars(
                        select(RunAction).where(
                            RunAction.run_id == pointer.run_id,
                            RunAction.tool == "review.publish_start",
                        )
                    )
                ).all()
                assert (
                    run is not None
                    and run.state is RunState.PUBLISHING
                    and run.lease_until is not None
                )
                self.committed_markers.append(len(markers))
            self.pointers.append(pointer)

    queue = Queue()
    original_t8 = SqlAlchemyReviewOutputRepository.enter_publishing_from_final

    async def fail_t8(self: SqlAlchemyReviewOutputRepository, run_id: UUID, **_: Any) -> bool:
        raise RuntimeError("T8 unavailable")

    async def scenario() -> tuple[str, list[str], FinalReviewResult, dict[str, Any]]:
        try:
            await prepare_result(factory, [code_snapshot()])

            def pipeline(attempt: int) -> ExecuteReviewRun:
                return ExecuteReviewRun(
                    Processor(),
                    Prompts(),
                    Model(),
                    StoreReviewOutput(
                        lambda: SqlAlchemyReviewOutputUnitOfWork(factory),
                        queue,
                        worker_id="worker",
                        expected_attempt=attempt,
                        run_url=lambda _: None,
                    ),
                    reflection_pass=ReflectionPass(verifier),
                )

            monkeypatch.setattr(
                SqlAlchemyReviewOutputRepository, "enter_publishing_from_final", fail_t8
            )
            with pytest.raises(RuntimeError, match="T8 unavailable"):
                await pipeline(1).execute(RUN_ATTEMPTED)
            assert "review.publish_start" not in [
                tool for _, tool, _, _ in await _actions(factory, RUN_ATTEMPTED)
            ]
            async with factory.begin() as session:
                run = await session.get(Run, RUN_ATTEMPTED)
                assert run is not None and run.state is RunState.RUNNING
                run.attempt = 2
            assert not await pipeline(1).execute(RUN_ATTEMPTED)
            monkeypatch.setattr(
                SqlAlchemyReviewOutputRepository, "enter_publishing_from_final", original_t8
            )
            async with factory.begin() as session:
                final = await session.get(FinalReviewResult, RUN_ATTEMPTED)
                assert final is not None
                saved_plan = final.publication_plan
                final.publication_plan = {**saved_plan, "head_sha": "0" * 40}
            assert not await pipeline(2).execute(RUN_ATTEMPTED)
            assert "review.publish_start" not in [
                tool for _, tool, _, _ in await _actions(factory, RUN_ATTEMPTED)
            ]
            async with factory.begin() as session:
                final = await session.get(FinalReviewResult, RUN_ATTEMPTED)
                assert final is not None
                final.publication_plan = saved_plan
            assert await pipeline(2).execute(RUN_ATTEMPTED)
            assert not await pipeline(2).execute(RUN_ATTEMPTED)
            async with factory() as session:
                run = await session.get(Run, RUN_ATTEMPTED)
                final = await session.get(FinalReviewResult, RUN_ATTEMPTED)
                assert run is not None and final is not None
                tools = list(
                    (
                        await session.scalars(
                            select(RunAction.tool)
                            .where(RunAction.run_id == RUN_ATTEMPTED)
                            .order_by(RunAction.index)
                        )
                    ).all()
                )
                marker = await session.scalar(
                    select(RunAction).where(
                        RunAction.run_id == RUN_ATTEMPTED,
                        RunAction.tool == "review.publish_start",
                    )
                )
                assert marker is not None
                return run.state.value, tools, final, marker.request
        finally:
            await engine.dispose()

    state, tools, final, marker_request = asyncio.run(scenario())
    assert state == "publishing" and final.source_attempt == 1
    assert reviewer_calls == verifier_calls == 1
    assert tools.count("llm.verify") == tools.count("review.finalize") == 1
    assert tools.count("review.publish_start") == 1
    assert tools[-2:] == ["review.finalize", "review.publish_start"]
    assert marker_request["attempt"] == 2 and marker_request["source_attempt"] == 1
    assert len(queue.pointers) == 1
    assert queue.committed_markers == [1]


@pytest.mark.integration
def test_llm_verify_is_written_before_the_postprocess_in_one_transaction(env: Env) -> None:
    engine = env.engine()
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async def scenario() -> tuple[
        list[tuple[int, str, Any, Any]], list[tuple[int, str]], list[tuple[str, datetime, int]]
    ]:
        try:
            run_id = await _run_ready_for_actions(factory)
            await _publish(factory, run_id, trace=True, commit=False)
            after_rollback = [(i, tool) for i, tool, _, _ in await _actions(factory, run_id)]
            await _publish(factory, run_id, trace=True)
            return (
                await _actions(factory, run_id),
                after_rollback,
                await _timings(factory, run_id),
            )
        finally:
            await engine.dispose()

    actions, after_rollback, timings = asyncio.run(scenario())
    assert [(index, tool) for index, tool, _, _ in actions] == [
        (0, "llm.review_output"),
        (1, "llm.verify"),
        (2, "review.postprocess"),
    ]
    assert actions[1][2] == VERIFY_REQUEST
    assert actions[1][3] == VERIFY_RESPONSE
    assert after_rollback == []
    # llm.verify reuses the publishing start with no duration of its own (verifier timing
    # lives in its llm.call rows); review.postprocess keeps its own duration.
    assert timings[1:] == [("llm.verify", STARTED, 0), ("review.postprocess", STARTED, 11)]


@pytest.mark.integration
def test_a_second_publishing_entry_adds_no_second_llm_verify(env: Env) -> None:
    engine = env.engine()
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async def scenario() -> list[tuple[int, str, Any, Any]]:
        try:
            run_id = await _run_ready_for_actions(factory)
            await _publish(factory, run_id, trace=True)
            await _publish(factory, run_id, trace=True)
            return await _actions(factory, run_id)
        finally:
            await engine.dispose()

    tools = [tool for _, tool, _, _ in asyncio.run(scenario())]
    assert tools.count("llm.verify") == 1
    assert tools[:3] == ["llm.review_output", "llm.verify", "review.postprocess"]


@pytest.mark.integration
def test_a_legacy_store_writes_no_llm_verify(env: Env) -> None:
    engine = env.engine()
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async def scenario() -> list[tuple[int, str, Any, Any]]:
        try:
            run_id = await _run_ready_for_actions(factory)
            await _publish(factory, run_id, trace=False)
            return await _actions(factory, run_id)
        finally:
            await engine.dispose()

    assert [(index, tool) for index, tool, _, _ in asyncio.run(scenario())] == [
        (0, "llm.review_output"),
        (1, "review.postprocess"),
    ]
