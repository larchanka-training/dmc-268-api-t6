"""Production repositories finalize durable policy-effective coverage on PostgreSQL."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.modules.reviews.application.diff_map import VcsProviderName, build_diff_map
from app.modules.reviews.application.get_run_diff import DiffSnapshot, StoreDiffSnapshot
from app.modules.reviews.application.prompt_builder import PullRequestMeta
from app.modules.reviews.application.review_ignore import (
    IgnoreMatcher,
    apply_ignore,
    parse_review_ignore,
)
from app.modules.reviews.application.run_coverage import project_run_coverage, snapshot_vcs_file
from app.modules.reviews.application.run_trace import TransactionalRunTrace
from app.modules.reviews.application.store_review_output import StoreReviewOutput
from app.modules.reviews.application.vcs_diff import PullRequestLocator, VcsPullRequest
from app.modules.reviews.infrastructure.models import Run, RunAction
from app.modules.reviews.infrastructure.review_output_unit_of_work import (
    SqlAlchemyReviewOutputUnitOfWork,
)
from app.modules.reviews.infrastructure.run_lifecycle_store import SqlAlchemyRunTraceUnitOfWork
from app.modules.reviews.infrastructure.run_processing_unit_of_work import (
    SqlAlchemyRunProcessingUnitOfWork,
)
from app.modules.reviews.infrastructure.run_repository import SqlAlchemyRunRepository
from tests.portal_postgres import HEAD, PR_NEW, REPO_A, RUN_ATTEMPTED, Env, portal_schema
from tests.test_clean_without_text_result import answer
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
                        RunAction.run_id == RUN_ATTEMPTED, RunAction.tool == "review.postprocess"
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
                        RunAction.run_id == RUN_ATTEMPTED, RunAction.tool == "review.postprocess"
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
                        RunAction.run_id == RUN_ATTEMPTED, RunAction.tool == "review.postprocess"
                    )
                )
                assert action is not None and isinstance(action.response, dict)
                assert action.response["verdict"] == (None if newer_invalid else "clean")
        finally:
            await engine.dispose()

    asyncio.run(scenario())
