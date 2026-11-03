"""Durable detail, latest-run, check-run and recovery projections stay consistent."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import event, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.modules.reviews.application.check_runs import check_run_view
from app.modules.reviews.application.get_run import GetRunDetail
from app.modules.reviews.application.get_run_diff import DiffSnapshot
from app.modules.reviews.application.list_pulls import ListRepositoryPulls
from app.modules.reviews.application.saved_review_result import read_saved_review_result
from app.modules.reviews.application.store_review_output import StoreReviewOutput
from app.modules.reviews.application.verdict import severity_counts
from app.modules.reviews.infrastructure.models import Finding, Run, RunAction, RunActionResponseBody
from app.modules.reviews.infrastructure.pull_request_queries import SqlAlchemyPullRequestQueries
from app.modules.reviews.infrastructure.review_output_unit_of_work import (
    SqlAlchemyReviewOutputUnitOfWork,
)
from app.modules.reviews.infrastructure.run_lifecycle_store import SqlAlchemyRunLifecycleStore
from app.modules.reviews.infrastructure.run_repository import SqlAlchemyRunRepository
from app.modules.reviews.infrastructure.saved_review_results import load_review_results
from tests.portal_postgres import HEAD, REPO_A, RUN_ATTEMPTED, Env
from tests.test_clean_without_text_result import answer
from tests.test_clean_without_text_result_postgres import prepare_result
from tests.test_clean_without_text_result_postgres import result_env as result_env
from tests.test_run_coverage import code_snapshot, empty_snapshot
from tests.test_severity_threshold_postgres import RecordingQueue


async def finalize_run(
    factory: async_sessionmaker[AsyncSession],
    snapshots: list[DiffSnapshot],
    *,
    output: dict[str, object] | None = None,
    expected_event: str | None = None,
) -> dict[str, Any]:
    await prepare_result(factory, snapshots, prompt_trace=True)
    queue = RecordingQueue()

    async def perform() -> None:
        assert await StoreReviewOutput(
            lambda: SqlAlchemyReviewOutputUnitOfWork(factory),
            queue,
            worker_id="worker",
            run_url=lambda _: None,
        ).execute(RUN_ATTEMPTED, answer() if output is None else output)

    await perform()
    if expected_event is not None:
        assert queue.pointers[0].review_event == expected_event
    async with factory.begin() as session:
        await session.execute(
            text("UPDATE runs SET lease_until=:expired WHERE id=:run"),
            {"expired": datetime.now(UTC) - timedelta(seconds=1), "run": RUN_ATTEMPTED},
        )
    async with factory() as session:
        recovered = await SqlAlchemyRunLifecycleStore(session).expired_publishing(
            datetime.now(UTC), 10
        )
        assert recovered == tuple(queue.pointers)
    async with SqlAlchemyReviewOutputUnitOfWork(factory) as uow:
        await uow.reviews.mark_review_published(RUN_ATTEMPTED)
        await uow.commit()
    async with factory() as session:
        action = await session.scalar(
            select(RunAction).where(
                RunAction.run_id == RUN_ATTEMPTED, RunAction.tool == "review.postprocess"
            )
        )
        assert action is not None and isinstance(action.response, dict)
        return action.response


@pytest.mark.integration
@pytest.mark.parametrize(
    ("other", "expected", "cause"),
    [
        (empty_snapshot(), "clean", None),
        (
            replace(empty_snapshot(), omission_reason="missing_patch"),
            None,
            "Нет подтверждения обычного пустого файла",
        ),
        (
            DiffSnapshot("src/moved.py", None, status="renamed", previous_filename="src/old.py"),
            None,
            "Отсутствующий patch не подтверждает неизменность содержимого переноса",
        ),
        (
            DiffSnapshot("src/run.sh", None, status="changed"),
            None,
            "Отсутствующий patch не подтверждает изменение только прав доступа/типа файла",
        ),
        (
            DiffSnapshot(
                "assets/gone.png", None, status="removed", omission_reason="missing_patch"
            ),
            None,
            "Удалённое содержимое недоступно для подтверждения binary",
        ),
        (
            DiffSnapshot("assets/large.png", None, omission_reason="too_large"),
            None,
            "Превышен лимит размера, содержимое не проверено",
        ),
    ],
)
def test_stored_result_detail_latest_pull_and_check_run_match_after_recovery(
    result_env: Env,
    other: DiffSnapshot,
    expected: str | None,
    cause: str | None,
) -> None:
    async def scenario() -> None:
        engine = result_env.engine()
        factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            saved = await finalize_run(factory, [code_snapshot(), other])
            detail = await GetRunDetail(
                SqlAlchemyRunRepository(factory, allow_unscoped=True)
            ).execute(RUN_ATTEMPTED)
            pulls = await ListRepositoryPulls(
                SqlAlchemyPullRequestQueries(factory, allow_unscoped=True)
            ).execute(REPO_A)
            async with factory() as session:
                report = await SqlAlchemyRunLifecycleStore(session).check_run_report(RUN_ATTEMPTED)
            assert detail is not None and pulls is not None and report is not None
            latest = next(item.latest_run for item in pulls.items if item.number == 3)
            assert latest is not None
            assert (
                detail.verdict == latest.verdict == report.verdict == saved["verdict"] == expected
            )
            expected_summary = {
                "problem": (
                    "Подтверждённых замечаний нет."
                    if cause is None
                    else "Подтверждённых замечаний нет; Проверка неполная: " + cause + " (1)."
                ),
                "done_well": (
                    "Проанализировано файлов с текстовыми изменениями: 1; исключено файлов: 1."
                    if cause is None
                    else "Проанализировано файлов с текстовыми изменениями: 1; исключено файлов: 0."
                ),
                "effort": "none",
            }
            assert detail.review.summary == saved["summary"] == expected_summary
            view = check_run_view(replace(report, run_url="https://portal.test/runs/213"))
            assert view.title == ("AI-ревью: clean" if expected else "AI-ревью: проверка неполная")
            assert expected_summary["problem"] in view.summary
            assert "https://portal.test/runs/213" in view.summary
            assert view.details_url == "https://portal.test/runs/213"
            if cause is not None:
                assert cause in view.summary
        finally:
            await engine.dispose()

    asyncio.run(scenario())


@pytest.mark.integration
@pytest.mark.parametrize("kind", ["referenced", "stale", "legacy", "newer_invalid"])
def test_views_use_referenced_body_and_conservatively_replace_stale_or_legacy_summary(
    result_env: Env,
    kind: str,
) -> None:
    async def scenario() -> None:
        engine = result_env.engine()
        factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            saved = await finalize_run(factory, [code_snapshot(), empty_snapshot()])
            async with factory.begin() as session:
                action = await session.scalar(
                    select(RunAction).where(
                        RunAction.run_id == RUN_ATTEMPTED, RunAction.tool == "review.postprocess"
                    )
                )
                assert action is not None
                if kind == "referenced":
                    body = RunActionResponseBody(run_id=RUN_ATTEMPTED, body=saved)
                    session.add(body)
                    await session.flush()
                    action.response_ref = str(body.id)
                    action.response = None
                elif kind == "stale":
                    action.response = saved | {
                        "coverage": saved["coverage"] | {"head_sha": "0" * 40}
                    }
                elif kind == "newer_invalid":
                    session.add(
                        RunAction(
                            run_id=RUN_ATTEMPTED,
                            index=action.index + 1,
                            tool="review.postprocess",
                            request={},
                            response={"coverage": {"version": 99}},
                            started_at=datetime.now(UTC),
                            duration_ms=0,
                        )
                    )
                else:
                    action.tool = "legacy.postprocess"
            detail = await GetRunDetail(
                SqlAlchemyRunRepository(factory, allow_unscoped=True)
            ).execute(RUN_ATTEMPTED)
            async with factory() as session:
                report = await SqlAlchemyRunLifecycleStore(session).check_run_report(RUN_ATTEMPTED)
            assert detail is not None and report is not None
            if kind == "referenced":
                assert detail.verdict == report.verdict == "clean"
                assert detail.review.summary == {
                    "problem": "Подтверждённых замечаний нет.",
                    "done_well": "Проанализировано файлов с текстовыми изменениями: 1; "
                    "исключено файлов: 1.",
                    "effort": "none",
                }
            else:
                assert detail.verdict is None and report.verdict is None
                assert (
                    "нет достоверного сохранённого покрытия"
                    in (detail.review.summary or {})["problem"]
                )
                assert "All changes were checked" not in repr(detail.review.summary)
        finally:
            await engine.dispose()

    asyncio.run(scenario())


@pytest.mark.integration
def test_latest_pull_page_queries_stay_batched_and_never_load_raw_patches(result_env: Env) -> None:
    async def scenario() -> None:
        engine = result_env.engine()
        factory = async_sessionmaker(engine, expire_on_commit=False)
        statements: list[str] = []

        def record(
            connection: object,
            cursor: object,
            statement: str,
            parameters: object,
            context: object,
            many: bool,
        ) -> None:
            statements.append(statement)

        try:
            await finalize_run(factory, [code_snapshot(), empty_snapshot()])
            event.listen(engine.sync_engine, "before_cursor_execute", record)
            queries = SqlAlchemyPullRequestQueries(factory, allow_unscoped=True)
            assert await queries.list_pulls(REPO_A, state="all", cursor=None, limit=1)
            single = len(statements)
            statements.clear()
            rows = await queries.list_pulls(REPO_A, state="all", cursor=None, limit=10)
            assert rows is not None and len(rows) == 3
            assert len(statements) <= single + 1
            assert not any(
                "code_change_diffs.patch" in sql or "code_change_diffs.review_patch" in sql
                for sql in statements
            )
        finally:
            await engine.dispose()

    asyncio.run(scenario())


@pytest.mark.integration
def test_legacy_fallback_preserves_exact_totals_with_twenty_metadata_rows(result_env: Env) -> None:
    async def scenario() -> None:
        engine = result_env.engine()
        factory = async_sessionmaker(engine, expire_on_commit=False)
        statements: list[str] = []

        def record(
            connection: object,
            cursor: object,
            statement: str,
            parameters: object,
            context: object,
            many: bool,
        ) -> None:
            statements.append(statement)

        try:
            await prepare_result(
                factory,
                [replace(code_snapshot(), filename=f"src/file{i:03}.py") for i in range(75)],
            )
            event.listen(engine.sync_engine, "before_cursor_execute", record)
            async with factory() as session:
                result = (await load_review_results(session, {RUN_ATTEMPTED: HEAD}, {}))[
                    RUN_ATTEMPTED
                ]
            assert len(statements) == 2
            assert not any(
                "code_change_diffs.patch" in sql or "code_change_diffs.review_patch" in sql
                for sql in statements
            )
            assert result.verdict is None
            assert result.coverage.total_files == 75
            assert result.coverage.reason_counts == {"classification_unavailable": 76}
            assert len(result.coverage.to_json()["reasons"]) == 20
            assert "отсутствует подтверждённый patch" not in result.summary["problem"]
            # Global diagnostic plus per-file unavailable evidence still conserves file totals.
            assert (
                read_saved_review_result(
                    {
                        "coverage": result.coverage.to_json(),
                        "verdict": None,
                        "summary": result.summary,
                        "severity_counts": severity_counts(()),
                    },
                    head_sha=HEAD,
                    severities=(),
                )
                is not None
            )
        finally:
            await engine.dispose()

    asyncio.run(scenario())


@pytest.mark.integration
@pytest.mark.parametrize("severity", ["high", "medium"])
def test_summary_only_withholds_risk_candidates_and_keeps_every_projection_null(
    result_env: Env,
    severity: str,
) -> None:
    async def scenario() -> None:
        engine = result_env.engine()
        factory = async_sessionmaker(engine, expire_on_commit=False)
        candidate = {
            "path": "src/app.py",
            "line": 1,
            "start_line": None,
            "severity": severity,
            "category": "security",
            "title": "Unchecked authorization",
            "body": "A remote caller can bypass the permission check.",
            "suggestion": None,
            "confidence": 0.9,
            "rule_name": None,
        }
        try:
            async with factory.begin() as session:
                await session.execute(
                    text("UPDATE repositories SET review_event='request_changes' WHERE id=:repo"),
                    {"repo": REPO_A},
                )
            # The durable summary-only flag makes the whole run an overview even if
            # another row still has a raw UI patch. Those rows cannot prove findings.
            saved = await finalize_run(
                factory,
                [
                    replace(code_snapshot(), summary_only=True),
                    replace(code_snapshot(), filename="src/helper.py"),
                ],
                output=answer() | {"findings": [candidate]},
                expected_event="COMMENT",
            )
            assert saved["verdict"] is None
            assert saved["coverage"]["status"] == "partial"
            assert saved["coverage"]["reviewed_files"] == 0
            assert saved["coverage"]["reason_counts"] == {"summary_only": 2}
            assert saved["severity_counts"] == severity_counts(())
            assert saved["inline"] == saved["body_only"] == 0
            assert saved["dropped"] == [{"index": 0, "drop_reason": "insufficient_context"}]
            detail = await GetRunDetail(
                SqlAlchemyRunRepository(factory, allow_unscoped=True)
            ).execute(RUN_ATTEMPTED)
            pulls = await ListRepositoryPulls(
                SqlAlchemyPullRequestQueries(factory, allow_unscoped=True)
            ).execute(REPO_A)
            async with factory() as session:
                report = await SqlAlchemyRunLifecycleStore(session).check_run_report(RUN_ATTEMPTED)
                body = await session.scalar(select(Run.review_body).where(Run.id == RUN_ATTEMPTED))
                finding = await session.scalar(
                    select(Finding).where(Finding.run_id == RUN_ATTEMPTED)
                )
            assert detail is not None and pulls is not None and report is not None
            latest = next(item.latest_run for item in pulls.items if item.number == 3)
            assert latest is not None
            assert detail.verdict is latest.verdict is report.verdict is None
            assert detail.review.findings == [] and report.inline_count == report.body_count == 0
            # A durable summary_only flag with no context.build reason is the size line.
            assert (
                detail.review.summary
                == report.saved_summary
                == saved["summary"]
                == {
                    "problem": "Построчное AI-ревью не выполнено: превышен лимит diff.",
                    "done_well": "Проанализировано файлов с текстовыми изменениями: 0; "
                    "исключено файлов: 0.",
                    "effort": "none",
                }
            )
            assert "превышен лимит diff" in saved["summary"]["problem"]
            assert body is not None and saved["summary"]["problem"] in body
            assert "**Verdict:** blocking" not in body and "**Verdict:** attention" not in body
            assert "Unchecked authorization" not in body
            assert finding is not None and finding.drop_reason == "insufficient_context"
            assert not finding.published
            view = check_run_view(report)
            assert view.title == "AI-ревью: только сводка" and view.conclusion == "neutral"
            assert saved["summary"]["problem"] in view.summary
        finally:
            await engine.dispose()

    asyncio.run(scenario())
