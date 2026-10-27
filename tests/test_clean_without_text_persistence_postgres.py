"""Public projections and bounded persisted coverage reads on PostgreSQL."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any, cast

import pytest
from sqlalchemy import event, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.common.infrastructure.db.enums import RunState
from app.modules.reviews.application.get_run import GetRunDetail
from app.modules.reviews.infrastructure.models import RunAction, RunActionResponseBody
from app.modules.reviews.infrastructure.pull_request_queries import SqlAlchemyPullRequestQueries
from app.modules.reviews.infrastructure.run_repository import (
    SqlAlchemyReviewOutputRepository,
    SqlAlchemyRunRepository,
)
from tests.portal_postgres import REPO_A, RUN_ATTEMPTED, RUN_B, Env
from tests.test_clean_without_text_result_postgres import prepare_result
from tests.test_clean_without_text_result_postgres import result_env as result_env
from tests.test_clean_without_text_views_postgres import finalize_run
from tests.test_run_coverage import code_snapshot, empty_snapshot


@pytest.mark.integration
@pytest.mark.parametrize("state", [state for state in RunState if state is not RunState.SUCCEEDED])
@pytest.mark.parametrize("saved", [False, True])
def test_nonsucceeded_details_withhold_summary_and_saved_result_without_loading_results(
    result_env: Env, state: RunState, saved: bool
) -> None:
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
            if saved:
                await finalize_run(factory, [code_snapshot(), empty_snapshot()])
            else:
                await prepare_result(factory, [code_snapshot(), empty_snapshot()])
            async with factory.begin() as session:
                await session.execute(
                    text("UPDATE runs SET state=:state WHERE id=:run"),
                    {"state": state.value, "run": RUN_ATTEMPTED},
                )
            event.listen(engine.sync_engine, "before_cursor_execute", record)
            detail = await GetRunDetail(
                SqlAlchemyRunRepository(factory, allow_unscoped=True)
            ).execute(RUN_ATTEMPTED)
            assert detail is not None
            assert detail.review.summary is None
            assert detail.review.saved_result is None
            assert detail.verdict is None
            assert not any("run_action_responses" in sql for sql in statements)
            assert not any("row_number()" in sql for sql in statements)
        finally:
            await engine.dispose()

    asyncio.run(scenario())


@pytest.mark.integration
def test_finalized_postgres_result_and_public_views_match_full_literal_json(
    result_env: Env,
) -> None:
    async def scenario() -> None:
        engine = result_env.engine()
        factory = async_sessionmaker(engine, expire_on_commit=False)
        expected = {
            "inline": 0,
            "body_only": 0,
            "omitted_body_only_count": 0,
            "dropped": [],
            "findings_hash": "4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945",
            "severity_counts": {"critical": 0, "high": 0, "medium": 0, "low": 0, "info": 0},
            "verdict": "clean",
            "summary": {
                "problem": "Подтверждённых замечаний нет.",
                "done_well": "Проанализировано файлов с текстовыми изменениями: 1; "
                "исключено файлов: 1.",
                "effort": "none",
            },
            "coverage": {
                "version": 1,
                "head_sha": "eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee",
                "diff_digest": "84e04c654253ca751e92426bb0b4d3fb435d2a64a9707294d7ac8471156768d4",
                "snapshot_digest": (
                    "e27dd3f4b1ed4a6dd92b5cce86c3147d5a3bc000d59434d9b7693ecb0a2a32df"
                ),
                "file_list_complete": True,
                "total_files": 2,
                "reviewed_files": 1,
                "status": "complete",
                "exclusion_counts": {"empty_file": 1},
                "reason_counts": {},
                "exclusions": [
                    {"path": "pkg/__init__.py", "reason": "empty_file", "change_type": "added"}
                ],
                "reasons": [],
            },
        }
        try:
            saved = await finalize_run(factory, [code_snapshot(), empty_snapshot()])
            assert saved == expected
            detail = await GetRunDetail(
                SqlAlchemyRunRepository(factory, allow_unscoped=True)
            ).execute(RUN_ATTEMPTED)
            rows = await SqlAlchemyPullRequestQueries(factory, allow_unscoped=True).list_pulls(
                REPO_A, state="all", cursor=None, limit=10
            )
            assert detail is not None and rows is not None
            assert detail.review.summary == expected["summary"]
            assert detail.review.saved_result is not None
            assert detail.review.saved_result.coverage.to_json() == expected["coverage"]
            latest = next(row.latest_run for row in rows if row.number == 3)
            assert latest is not None and latest.saved_result is not None
            assert latest.saved_result.summary == expected["summary"]
            assert latest.saved_result.coverage.to_json() == expected["coverage"]
        finally:
            await engine.dispose()

    asyncio.run(scenario())


@pytest.mark.integration
@pytest.mark.parametrize("latest", ["prompt", "null", "invalid"])
@pytest.mark.parametrize("referenced", [False, True])
def test_context_restore_selects_one_explicit_classification_among_large_prompt_bodies(
    result_env: Env, latest: str, referenced: bool
) -> None:
    async def scenario() -> None:
        engine = result_env.engine()
        factory = async_sessionmaker(engine, expire_on_commit=False)
        body_rows: list[int] = []

        def record(
            connection: object,
            cursor: object,
            statement: str,
            parameters: object,
            context: object,
            many: bool,
        ) -> None:
            if "run_actions.request" in statement and "run_action_responses.body" in statement:
                body_rows.append(cast(Any, cursor).rowcount)

        try:
            await prepare_result(
                factory, [code_snapshot(), empty_snapshot()], referenced=referenced
            )
            async with factory.begin() as session:
                for index in range(2, 27):
                    response: dict[str, object] = {"prompt": "x" * 65536}
                    if index == 26 and latest != "prompt":
                        response["coverage"] = None if latest == "null" else {"version": 99}
                    action = RunAction(
                        run_id=RUN_ATTEMPTED,
                        index=index,
                        tool="context.build",
                        request={"head_sha": "e" * 40},
                        response=None if referenced else response,
                        started_at=datetime.now(UTC),
                        duration_ms=0,
                    )
                    if referenced:
                        body = RunActionResponseBody(run_id=RUN_ATTEMPTED, body=response)
                        session.add(body)
                        await session.flush()
                        action.response_ref = str(body.id)
                    session.add(action)
            event.listen(engine.sync_engine, "after_cursor_execute", record)
            async with factory() as session:
                post_input = await SqlAlchemyReviewOutputRepository(
                    session
                ).get_post_processing_input(RUN_ATTEMPTED)
            assert post_input is not None and post_input.coverage is not None
            assert post_input.coverage.status.value == (
                "complete" if latest == "prompt" else "partial"
            )
            assert post_input.diff_map.diff_old_sha == ("c" * 40 if latest == "prompt" else None)
            assert body_rows == [1]
            if latest != "prompt":
                assert all(item.path is None for item in post_input.coverage.reasons)
        finally:
            await engine.dispose()

    asyncio.run(scenario())


@pytest.mark.integration
def test_referenced_result_and_raw_response_cannot_read_another_runs_body(result_env: Env) -> None:
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
                foreign_body = RunActionResponseBody(run_id=RUN_B, body=saved)
                session.add(foreign_body)
                await session.flush()
                action.response_ref = str(foreign_body.id)
                action.response = None
                index = action.index
            repository = SqlAlchemyRunRepository(factory, allow_unscoped=True)
            detail = await GetRunDetail(repository).execute(RUN_ATTEMPTED)
            raw = await repository.get_run_action_response(RUN_ATTEMPTED, index)
            assert detail is not None and raw is not None
            assert detail.verdict is None
            assert detail.review.summary == {
                "problem": "Подтверждённых замечаний нет; Проверка неполная: "
                "нет достоверного сохранённого покрытия (3).",
                "done_well": "Проанализировано файлов с текстовыми изменениями: 0; "
                "исключено файлов: 0.",
                "effort": "none",
            }
            assert raw.response is None
        finally:
            await engine.dispose()

    asyncio.run(scenario())


@pytest.mark.integration
def test_latest_nonsucceeded_runs_skip_saved_result_and_fallback_queries(result_env: Env) -> None:
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
            async with factory.begin() as session:
                await session.execute(text("UPDATE runs SET state='running'"))
            event.listen(engine.sync_engine, "before_cursor_execute", record)
            rows = await SqlAlchemyPullRequestQueries(factory, allow_unscoped=True).list_pulls(
                REPO_A, state="all", cursor=None, limit=10
            )
            assert rows is not None and len(rows) == 3
            assert all(row.latest_run is not None for row in rows)
            assert all(row.latest_run.saved_result is None for row in rows if row.latest_run)
            assert not any("run_action_responses" in sql for sql in statements)
            assert not any("row_number()" in sql for sql in statements)
        finally:
            await engine.dispose()

    asyncio.run(scenario())
