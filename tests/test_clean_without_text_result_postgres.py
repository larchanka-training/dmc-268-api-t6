"""Production repositories finalize durable policy-effective coverage on PostgreSQL."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.common.infrastructure.db.enums import ReviewEvent
from app.modules.repositories.infrastructure.models import Repository
from app.modules.reviews.application.check_runs import RunProviderCheckpoint
from app.modules.reviews.application.diff_map import VcsProviderName, build_diff_map
from app.modules.reviews.application.get_run_diff import DiffSnapshot, StoreDiffSnapshot
from app.modules.reviews.application.prompt_builder import PullRequestMeta, file_block_id
from app.modules.reviews.application.publish_run_review import (
    PublishRunReview,
    ReviewSubmission,
    SubmittedReview,
)
from app.modules.reviews.application.review_ignore import (
    IgnoreMatcher,
    apply_ignore,
    parse_review_ignore,
)
from app.modules.reviews.application.review_output import VerifiedReviewOutput
from app.modules.reviews.application.run_coverage import project_run_coverage, snapshot_vcs_file
from app.modules.reviews.application.run_trace import TransactionalRunTrace
from app.modules.reviews.application.store_review_output import NoModelAnswer, StoreReviewOutput
from app.modules.reviews.application.vcs_diff import PullRequestLocator, VcsPullRequest
from app.modules.reviews.infrastructure.models import (
    Comment,
    FinalReviewResult,
    Finding,
    Run,
    RunAction,
)
from app.modules.reviews.infrastructure.review_output_unit_of_work import (
    SqlAlchemyReviewOutputUnitOfWork,
)
from app.modules.reviews.infrastructure.run_lifecycle_store import (
    SqlAlchemyRunLifecycleStore,
    SqlAlchemyRunLifecycleUnitOfWork,
    SqlAlchemyRunTraceUnitOfWork,
)
from app.modules.reviews.infrastructure.run_processing_unit_of_work import (
    SqlAlchemyRunProcessingUnitOfWork,
)
from app.modules.reviews.infrastructure.run_repository import (
    SqlAlchemyReviewOutputRepository,
    SqlAlchemyRunRepository,
)
from tests.portal_postgres import HEAD, PR_NEW, REPO_A, RUN_ATTEMPTED, Env, portal_schema
from tests.test_clean_without_text_result import answer
from tests.test_clean_without_text_sent_context import finding
from tests.test_run_coverage import code_snapshot, empty_snapshot
from tests.test_severity_threshold_postgres import RecordingQueue


@pytest.fixture
def result_env() -> Iterator[Env]:
    database_url = os.environ.get("TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("set TEST_DATABASE_URL to run PostgreSQL integration tests")
    with portal_schema(database_url, None) as env:
        yield env


async def prepare_result(
    factory: async_sessionmaker[AsyncSession],
    snapshots: list[DiffSnapshot],
    *,
    rules: str = "",
    stale: bool = False,
    referenced: bool = False,
    prompt_trace: bool = False,
) -> None:
    async with factory.begin() as session:
        await session.execute(
            text("UPDATE runs SET state='running', worker_id='worker' WHERE id=:run"),
            {"run": RUN_ATTEMPTED},
        )
    async with SqlAlchemyRunProcessingUnitOfWork(factory) as uow:
        await StoreDiffSnapshot(uow.repository).execute(
            run_id=RUN_ATTEMPTED, code_change_id=PR_NEW, head_sha=HEAD, files=snapshots
        )
        await uow.commit()
    stored = await SqlAlchemyRunRepository(factory, allow_unscoped=True).get_run_snapshots(
        RUN_ATTEMPTED
    )
    assert stored is not None
    pull = VcsPullRequest(
        PullRequestLocator(17, "octo/a", 3),
        HEAD,
        "b" * 40,
        PullRequestMeta("PR", None, "octo", "feature", "main", (), len(stored), 1, 1, False, False),
    )
    diff = build_diff_map(
        pull,
        tuple(snapshot_vcs_file(item) for item in stored),
        provider=VcsProviderName.GITHUB,
        repository_id=REPO_A,
        diff_old_sha="c" * 40,
    )
    ignore = apply_ignore(IgnoreMatcher(parse_review_ignore(rules)), diff)
    saved = project_run_coverage(stored, diff, ignore=ignore).to_json()
    if stale:
        saved["head_sha"] = "0" * 40
    response: dict[str, Any] = {
        "diff": {"digest": diff.digest, "diff_old_sha": "c" * 40, "file_list_complete": True},
        "coverage": saved,
    }
    if referenced:
        response["optional_padding"] = "x" * 65536
    await TransactionalRunTrace(lambda: SqlAlchemyRunTraceUnitOfWork(factory)).record(
        RUN_ATTEMPTED, "context.build", {"head_sha": HEAD}, response, datetime.now(UTC), 0
    )
    if prompt_trace:
        await TransactionalRunTrace(lambda: SqlAlchemyRunTraceUnitOfWork(factory)).record(
            RUN_ATTEMPTED,
            "context.build",
            {"engine": "fast"},
            {"files": [], "budget": {}},
            datetime.now(UTC),
            0,
        )


@pytest.mark.integration
@pytest.mark.parametrize(
    ("other", "expected"),
    [
        (empty_snapshot(), "clean"),
        (replace(empty_snapshot(), omission_reason="missing_patch"), None),
    ],
)
def test_postgres_snapshot_reload_and_finalization_persist_matching_result_summary_and_body(
    result_env: Env,
    other: DiffSnapshot,
    expected: str | None,
) -> None:
    async def scenario() -> None:
        engine = result_env.engine()
        factory = async_sessionmaker(engine, expire_on_commit=False)
        queue = RecordingQueue()
        try:
            await prepare_result(factory, [code_snapshot(), other])
            assert (
                await StoreReviewOutput(
                    lambda: SqlAlchemyReviewOutputUnitOfWork(factory),
                    queue,
                    worker_id="worker",
                    run_url=lambda _: None,
                ).execute(RUN_ATTEMPTED, answer())
                is True
            )
            async with factory() as session:
                action = await session.scalar(
                    select(RunAction).where(
                        RunAction.run_id == RUN_ATTEMPTED, RunAction.tool == "review.finalize"
                    )
                )
                run = await session.get(Run, RUN_ATTEMPTED)
                assert action is not None and run is not None and run.review_body is not None
                assert isinstance(action.response, dict)
                assert action.response["verdict"] == expected
                assert action.response["coverage"]["status"] == (
                    "complete" if expected else "partial"
                )
                assert ("**Verdict:** clean" in run.review_body) is (expected == "clean")
                if expected is None:
                    assert "Проверка неполная" in action.response["summary"]["problem"]
                    assert "All changes were checked" not in run.review_body
            assert len(queue.pointers) == 1
        finally:
            await engine.dispose()

    asyncio.run(scenario())


@pytest.mark.integration
@pytest.mark.parametrize("backend_only", [False, True])
def test_final_result_commits_in_running_before_publication_transition(
    result_env: Env, monkeypatch: pytest.MonkeyPatch, backend_only: bool
) -> None:
    async def fail_t8(self: SqlAlchemyReviewOutputRepository, run_id: Any, **_: Any) -> None:
        raise RuntimeError("T8 unavailable")

    monkeypatch.setattr(SqlAlchemyReviewOutputRepository, "enter_publishing", fail_t8)

    async def scenario() -> tuple[str, list[str], dict[str, Any] | None, FinalReviewResult]:
        engine = result_env.engine()
        factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            await prepare_result(factory, [code_snapshot()])
            for _ in range(2):
                with pytest.raises(RuntimeError, match="T8 unavailable"):
                    await StoreReviewOutput(
                        lambda: SqlAlchemyReviewOutputUnitOfWork(factory),
                        RecordingQueue(),
                        worker_id="worker",
                        expected_attempt=1,
                        run_url=lambda _: None,
                    ).execute(RUN_ATTEMPTED, NoModelAnswer() if backend_only else answer())
            async with SqlAlchemyReviewOutputUnitOfWork(factory) as uow:
                await uow.reviews.enter_publishing_from_final(
                    RUN_ATTEMPTED, lease_until=datetime.now(UTC) + timedelta(minutes=5)
                )
                await uow.rollback()
            async with factory() as session:
                run = await session.get(Run, RUN_ATTEMPTED)
                assert run is not None
                final_result = await session.get(FinalReviewResult, RUN_ATTEMPTED)
                assert final_result is not None
                actions = list(
                    (
                        await session.scalars(
                            select(RunAction)
                            .where(RunAction.run_id == RUN_ATTEMPTED)
                            .order_by(RunAction.index)
                        )
                    ).all()
                )
                finalize = next(
                    (action for action in actions if action.tool == "review.finalize"), None
                )
                return (
                    run.state.value,
                    [action.tool for action in actions],
                    None if finalize is None else finalize.request,
                    final_result,
                )
        finally:
            await engine.dispose()

    state, actions, request, final_result = asyncio.run(scenario())
    assert state == "running"
    assert actions.count("review.finalize") == 1
    assert "review.publish_start" not in actions
    assert ("llm.review_output" in actions) is not backend_only
    assert request is not None and request["accepted_review_call_id"] is None
    assert request["accepted_verifier_call_id"] is None
    assert len(request["digest"]) == 64
    assert final_result.source_attempt == 1
    assert final_result.result["findings"] == []
    assert final_result.publication_plan["findings"] == []
    assert final_result.publication_plan["review_body"]


@pytest.mark.integration
@pytest.mark.parametrize("failure", ["write", "cancel"])
def test_finalization_failure_or_cancel_leaves_no_partial_result(
    result_env: Env, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    original = SqlAlchemyReviewOutputRepository.finalize_review

    async def fail_after_write(
        self: SqlAlchemyReviewOutputRepository, run_id: Any, **kwargs: Any
    ) -> None:
        await original(self, run_id, **kwargs)
        raise RuntimeError("finalization failed")

    if failure == "write":
        monkeypatch.setattr(SqlAlchemyReviewOutputRepository, "finalize_review", fail_after_write)

    async def scenario() -> tuple[str, str | None, FinalReviewResult | None, list[str]]:
        engine = result_env.engine()
        factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            await prepare_result(factory, [code_snapshot()])
            if failure == "cancel":
                async with factory.begin() as session:
                    run = await session.get(Run, RUN_ATTEMPTED)
                    assert run is not None
                    run.cancel_requested = True
            store = StoreReviewOutput(
                lambda: SqlAlchemyReviewOutputUnitOfWork(factory),
                RecordingQueue(),
                worker_id="worker",
                expected_attempt=1,
                run_url=lambda _: None,
            )
            if failure == "write":
                with pytest.raises(RuntimeError, match="finalization failed"):
                    await store.execute(RUN_ATTEMPTED, answer())
            else:
                assert not await store.execute(RUN_ATTEMPTED, answer())
            async with factory() as session:
                run = await session.get(Run, RUN_ATTEMPTED)
                assert run is not None
                final_result = await session.get(FinalReviewResult, RUN_ATTEMPTED)
                tools = list(
                    (
                        await session.scalars(
                            select(RunAction.tool).where(RunAction.run_id == RUN_ATTEMPTED)
                        )
                    ).all()
                )
                return run.state.value, run.review_body, final_result, tools
        finally:
            await engine.dispose()

    state, body, final_result, tools = asyncio.run(scenario())
    assert state == "running" and body is None and final_result is None
    assert "review.finalize" not in tools
    assert "review.publish_start" not in tools


@pytest.mark.integration
def test_final_result_and_plan_contain_only_supported_findings(result_env: Env) -> None:
    async def scenario() -> tuple[FinalReviewResult, list[str], str, str, str, str]:
        engine = result_env.engine()
        factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            await prepare_result(factory, [code_snapshot()])
            supported = {
                **finding("src/app.py", "high"),
                "evidence_refs": [
                    {"block_id": file_block_id("src/app.py"), "start_line": 1, "end_line": 1}
                ],
            }
            rejected = {
                **finding("src/app.py", "medium"),
                "title": "Rejected candidate",
                "evidence_refs": [
                    {"block_id": file_block_id("src/app.py"), "start_line": 1, "end_line": 1}
                ],
            }
            store = StoreReviewOutput(
                lambda: SqlAlchemyReviewOutputUnitOfWork(factory),
                RecordingQueue(),
                worker_id="worker",
                expected_attempt=1,
                run_url=lambda _: None,
            )
            assert await store.checkpoint_candidates(
                RUN_ATTEMPTED,
                {"findings": [supported, rejected], "summary": answer()["summary"]},
                ("c1", "c2"),
                candidates=[supported, rejected],
                dropped=[],
                accepted_call_id=UUID(int=435),
            )
            output = VerifiedReviewOutput(
                {"findings": [supported], "summary": answer()["summary"]},
                dropped_findings=[
                    {"finding": rejected, "drop_reason": "contradicted", "position": 1}
                ],
                verify_request={
                    "candidate_ids": ["c1", "c2"],
                    "accepted_call_id": str(UUID(int=436)),
                    "manifest_id": None,
                },
                verify_response={
                    "decisions": [
                        {"candidate_id": "c1", "status": "supported"},
                        {"candidate_id": "c2", "status": "contradicted"},
                    ]
                },
            )
            assert await store.execute(RUN_ATTEMPTED, output)
            async with factory() as session:
                final_result = await session.get(FinalReviewResult, RUN_ATTEMPTED)
                assert final_result is not None
                tools = list(
                    (
                        await session.scalars(
                            select(RunAction.tool)
                            .where(RunAction.run_id == RUN_ATTEMPTED)
                            .order_by(RunAction.index)
                        )
                    ).all()
                )
            async with factory.begin() as session:
                run = await session.get(Run, RUN_ATTEMPTED)
                inline = await session.scalar(
                    select(Finding).where(
                        Finding.run_id == RUN_ATTEMPTED, Finding.inline_comment.is_(True)
                    )
                )
                assert run is not None and inline is not None
                run.review_body = "mutable body changed"
                inline.title = "Mutable title changed"
            async with factory() as session:
                lifecycle = SqlAlchemyRunLifecycleStore(session)
                context = await lifecycle.publish_context(RUN_ATTEMPTED)
                assert context is not None and len(context.findings) == 1
                pointers = await lifecycle.expired_publishing(
                    datetime.now(UTC) + timedelta(hours=1), 10
                )
                assert len(pointers) == 1
                return (
                    final_result,
                    tools,
                    context.review_body,
                    context.findings[0].title,
                    context.findings_hash,
                    pointers[0].findings_hash,
                )
        finally:
            await engine.dispose()

    final_result, tools, body, title, hash_, pointer_hash = asyncio.run(scenario())
    assert final_result.accepted_review_call_id == UUID(int=435)
    assert final_result.accepted_verifier_call_id == UUID(int=436)
    assert [item["title"] for item in final_result.result["findings"]] == [
        "Unchecked authorization"
    ]
    assert [item["title"] for item in final_result.publication_plan["findings"]] == [
        "Unchecked authorization"
    ]
    assert (body, title, hash_, pointer_hash) == (
        final_result.publication_plan["review_body"],
        "Unchecked authorization",
        final_result.publication_plan["findings_hash"],
        final_result.publication_plan["findings_hash"],
    )
    assert tools[-5:] == [
        "llm.review_output",
        "review.postprocess",
        "llm.verify",
        "review.finalize",
        "review.publish_start",
    ]


@pytest.mark.integration
def test_comment_receipts_follow_saved_plan_order_not_finding_row_order(result_env: Env) -> None:
    async def scenario() -> tuple[list[str], list[tuple[str, int]]]:
        engine = result_env.engine()
        factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            await prepare_result(
                factory, [code_snapshot(), replace(code_snapshot(), filename="src/other.py")]
            )
            candidates = [
                {
                    **finding(path),
                    "title": title,
                    "evidence_refs": [
                        {"block_id": file_block_id(path), "start_line": 1, "end_line": 1}
                    ],
                }
                for path, title in (
                    ("src/app.py", "First submitted"),
                    ("src/other.py", "Second submitted"),
                )
            ]
            store = StoreReviewOutput(
                lambda: SqlAlchemyReviewOutputUnitOfWork(factory),
                RecordingQueue(),
                worker_id="worker",
                expected_attempt=1,
                run_url=lambda _: None,
            )
            assert await store.execute(
                RUN_ATTEMPTED,
                VerifiedReviewOutput({"findings": candidates, "summary": answer()["summary"]}),
            )
            async with factory.begin() as session:
                saved = await session.get(FinalReviewResult, RUN_ATTEMPTED)
                assert saved is not None
                plan = saved.publication_plan
                first, second = [UUID(item["finding_id"]) for item in plan["findings"]]
                first_row = await session.get(Finding, first)
                second_row = await session.get(Finding, second)
                assert first_row is not None and second_row is not None
                first_row.created_at = datetime(2026, 11, 4, tzinfo=UTC)
                second_row.created_at = datetime(2026, 11, 2, tzinfo=UTC)
            async with SqlAlchemyRunLifecycleUnitOfWork(factory) as uow:
                context = await uow.runs.publish_context(RUN_ATTEMPTED)
                assert context is not None
                assert (
                    await uow.runs.complete_publication(
                        RUN_ATTEMPTED,
                        review_id=55,
                        comment_ids=(101, 202),
                        findings_hash=context.findings_hash,
                        now=datetime.now(UTC),
                        expected_publication=context,
                    )
                    == "succeeded"
                )
                await uow.commit()
            async with factory() as session:
                receipts = (
                    await session.execute(
                        select(Finding.title, Comment.github_comment_id)
                        .join(Comment, Comment.finding_id == Finding.id)
                        .where(Finding.run_id == RUN_ATTEMPTED)
                    )
                ).all()
                return [item["title"] for item in plan["findings"]], [
                    tuple(row) for row in receipts
                ]
        finally:
            await engine.dispose()

    titles, receipts = asyncio.run(scenario())
    assert titles == ["First submitted", "Second submitted"]
    assert dict(receipts) == {"First submitted": 101, "Second submitted": 202}


@pytest.mark.integration
def test_t8_queue_pointer_keeps_final_plan_event_after_repository_setting_changes(
    result_env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = result_env.engine()
    factory = async_sessionmaker(engine, expire_on_commit=False)
    original_finalize = SqlAlchemyReviewOutputRepository.finalize_review

    async def change_setting(
        self: SqlAlchemyReviewOutputRepository, run_id: UUID, **kwargs: Any
    ) -> None:
        await original_finalize(self, run_id, **kwargs)
        repository = await self._session.get(Repository, REPO_A)
        assert repository is not None
        repository.review_event = ReviewEvent.COMMENT

    monkeypatch.setattr(SqlAlchemyReviewOutputRepository, "finalize_review", change_setting)

    async def scenario() -> tuple[str, str]:
        try:
            await prepare_result(factory, [code_snapshot()])
            async with factory.begin() as session:
                repository = await session.get(Repository, REPO_A)
                assert repository is not None
                repository.review_event = ReviewEvent.REQUEST_CHANGES
            candidate = {
                **finding("src/app.py"),
                "evidence_refs": [
                    {"block_id": file_block_id("src/app.py"), "start_line": 1, "end_line": 1}
                ],
            }
            queue = RecordingQueue()
            assert await StoreReviewOutput(
                lambda: SqlAlchemyReviewOutputUnitOfWork(factory),
                queue,
                worker_id="worker",
                expected_attempt=1,
                run_url=lambda _: None,
            ).execute(
                RUN_ATTEMPTED,
                VerifiedReviewOutput({"findings": [candidate], "summary": answer()["summary"]}),
            )
            async with factory() as session:
                saved = await session.get(FinalReviewResult, RUN_ATTEMPTED)
                assert saved is not None and len(queue.pointers) == 1
                return saved.publication_plan["review_event"], queue.pointers[0].review_event
        finally:
            await engine.dispose()

    assert asyncio.run(scenario()) == ("REQUEST_CHANGES", "REQUEST_CHANGES")


@pytest.mark.integration
def test_publish_start_distinguishes_crash_before_and_after_provider_action(
    result_env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = result_env.engine()
    factory = async_sessionmaker(engine, expire_on_commit=False)

    class Reviews:
        calls = 0
        events: list[str] = []

        async def submit_review(
            self, submission: ReviewSubmission, *, checkpoint: RunProviderCheckpoint
        ) -> SubmittedReview:
            self.calls += 1
            self.events.append(submission.event)
            await checkpoint()
            return SubmittedReview(55, ())

    reviews = Reviews()
    queue = RecordingQueue()
    original_complete = SqlAlchemyRunLifecycleStore.complete_publication

    async def fail_complete(self: SqlAlchemyRunLifecycleStore, run_id: UUID, **_: Any) -> str:
        raise RuntimeError("completion unavailable")

    async def tools() -> list[str]:
        async with factory() as session:
            return list(
                (
                    await session.scalars(
                        select(RunAction.tool)
                        .where(RunAction.run_id == RUN_ATTEMPTED)
                        .order_by(RunAction.index)
                    )
                ).all()
            )

    async def scenario() -> tuple[list[str], list[str], list[str], str]:
        try:
            await prepare_result(factory, [code_snapshot()])
            store = StoreReviewOutput(
                lambda: SqlAlchemyReviewOutputUnitOfWork(factory),
                queue,
                worker_id="worker",
                expected_attempt=1,
                run_url=lambda _: None,
            )
            assert await store.execute(RUN_ATTEMPTED, NoModelAnswer())
            before = await tools()
            publisher = PublishRunReview(
                uow_factory=lambda: SqlAlchemyRunLifecycleUnitOfWork(factory),
                reviews=reviews,
                check_runs=None,
                trace=TransactionalRunTrace(lambda: SqlAlchemyRunTraceUnitOfWork(factory)),
            )
            await publisher.execute(replace(queue.pointers[0], review_event="REQUEST_CHANGES"))
            assert reviews.calls == 0
            monkeypatch.setattr(SqlAlchemyRunLifecycleStore, "complete_publication", fail_complete)
            with pytest.raises(RuntimeError, match="completion unavailable"):
                await publisher.execute(queue.pointers[0])
            after = await tools()
            monkeypatch.setattr(
                SqlAlchemyRunLifecycleStore, "complete_publication", original_complete
            )
            await publisher.execute(queue.pointers[0])
            await publisher.execute(queue.pointers[0])
            async with factory() as session:
                run = await session.get(Run, RUN_ATTEMPTED)
                assert run is not None
                return before, after, await tools(), run.state.value
        finally:
            await engine.dispose()

    before, after, completed, state = asyncio.run(scenario())
    assert before[-1] == "review.publish_start"
    assert "github.publish_review" not in before
    assert after.count("review.publish_start") == after.count("github.publish_review") == 1
    assert completed.count("review.publish_start") == 1
    assert completed.count("github.publish_review") == 2
    assert reviews.calls == 2 and reviews.events == ["COMMENT", "COMMENT"]
    assert state == "succeeded"


@pytest.mark.integration
@pytest.mark.parametrize(("stale", "expected"), [(False, "clean"), (True, None)])
def test_postgres_finalization_reads_referenced_policy_effective_coverage_and_rejects_stale_head(
    result_env: Env,
    stale: bool,
    expected: str | None,
) -> None:
    async def scenario() -> None:
        engine = result_env.engine()
        factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            await prepare_result(
                factory,
                [
                    code_snapshot(),
                    DiffSnapshot("private/hidden.py", None, omission_reason="missing_patch"),
                ],
                rules="```review-ignore\nprivate/**\n```\n",
                stale=stale,
                referenced=True,
            )
            queue = RecordingQueue()
            assert await StoreReviewOutput(
                lambda: SqlAlchemyReviewOutputUnitOfWork(factory),
                queue,
                worker_id="worker",
                run_url=lambda _: None,
            ).execute(RUN_ATTEMPTED, answer())
            async with factory() as session:
                action = await session.scalar(
                    select(RunAction).where(
                        RunAction.run_id == RUN_ATTEMPTED, RunAction.tool == "review.finalize"
                    )
                )
                assert action is not None
                assert isinstance(action.response, dict)
                assert action.response["verdict"] == expected
                if stale:
                    assert (
                        "classification_unavailable" in action.response["coverage"]["reason_counts"]
                    )
        finally:
            await engine.dispose()

    asyncio.run(scenario())


@pytest.mark.integration
@pytest.mark.parametrize("newer_invalid", [False, True])
def test_later_prompt_context_trace_does_not_erase_current_identity_valid_classification(
    result_env: Env,
    newer_invalid: bool,
) -> None:
    async def scenario() -> None:
        engine = result_env.engine()
        factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            await prepare_result(factory, [code_snapshot(), empty_snapshot()], prompt_trace=True)
            if newer_invalid:
                await TransactionalRunTrace(lambda: SqlAlchemyRunTraceUnitOfWork(factory)).record(
                    RUN_ATTEMPTED,
                    "context.build",
                    {"head_sha": HEAD},
                    {"coverage": {"version": 2}, "diff": {}},
                    datetime.now(UTC),
                    0,
                )
            queue = RecordingQueue()
            assert await StoreReviewOutput(
                lambda: SqlAlchemyReviewOutputUnitOfWork(factory),
                queue,
                worker_id="worker",
                run_url=lambda _: None,
            ).execute(RUN_ATTEMPTED, answer())
            async with factory() as session:
                action = await session.scalar(
                    select(RunAction).where(
                        RunAction.run_id == RUN_ATTEMPTED, RunAction.tool == "review.finalize"
                    )
                )
                assert action is not None and isinstance(action.response, dict)
                assert action.response["verdict"] == (None if newer_invalid else "clean")
        finally:
            await engine.dispose()

    asyncio.run(scenario())
