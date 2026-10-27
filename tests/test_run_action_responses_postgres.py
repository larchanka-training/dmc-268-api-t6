"""Run action responses over 64 KB stored by reference (#34, D1)."""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Iterator
from functools import partial

import pytest

from app.modules.auth.application.scope import AuthScope
from app.modules.reviews.application.conventions import CachedConventions, ConventionsFile
from app.modules.reviews.application.run_coverage import CoverageStatus, RunCoverage
from app.modules.reviews.application.run_trace import TransactionalRunTrace
from app.modules.reviews.infrastructure.conventions_unit_of_work import (
    SqlAlchemyRepositoryConventionsUnitOfWork,
)
from app.modules.reviews.infrastructure.run_action_payloads import ROW_LIMIT_BYTES
from app.modules.reviews.infrastructure.run_lifecycle_store import (
    SqlAlchemyRunTraceUnitOfWork,
)
from tests.portal_postgres import (
    HEAD,
    NOW,
    PROMPT,
    REPO_A,
    RUN_DONE,
    WS_A,
    Env,
    api,
    portal_schema,
    scalar,
)


@pytest.fixture
def env() -> Iterator[Env]:
    database_url = os.environ.get("TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("set TEST_DATABASE_URL to run PostgreSQL integration tests")
    with portal_schema(database_url, None) as schema:
        yield schema


@pytest.mark.integration
def test_large_action_responses_are_stored_by_reference_and_truncated_over_the_limit(
    env: Env,
) -> None:
    large = {"text": "x" * (70 * 1024)}
    huge = {"text": "y" * (ROW_LIMIT_BYTES + 10)}
    with api(env, AuthScope(42, (WS_A,))) as (client, factory):
        trace = TransactionalRunTrace(partial(SqlAlchemyRunTraceUnitOfWork, factory))
        for response in ({"small": True}, large, huge):
            asyncio.run(trace.record(RUN_DONE, "context.build", {}, response, NOW, 1))
        actions = client.get(f"/api/runs/{RUN_DONE}/actions").json()
        bodies = [
            client.get(f"/api/runs/{RUN_DONE}/actions/{index}/response").json()
            for index in (0, 1, 2)
        ]
        stored = scalar(
            factory,
            "SELECT count(*), bool_and(response IS NULL) FROM run_actions "
            "WHERE run_id = :id AND response_ref IS NOT NULL",
            id=RUN_DONE,
        )

    assert tuple(stored) == (2, True)
    assert actions[0]["response"] == {"small": True} and actions[0]["responseRef"] is None
    assert [action["responseRef"] for action in actions[1:]] == [
        f"/api/runs/{RUN_DONE}/actions/1/response",
        f"/api/runs/{RUN_DONE}/actions/2/response",
    ]
    assert bodies[1] == large
    assert bodies[2]["truncated"] is True
    assert bodies[2]["original_bytes"] == len(json.dumps(huge, separators=(",", ":")).encode())
    assert huge["text"].startswith(bodies[2]["text"][len('{"text":"') :])


@pytest.mark.integration
def test_large_repo_conventions_trace_is_stored_by_reference(env: Env) -> None:
    files = tuple(
        ConventionsFile(path=f"src/module_{index}.py", relevance="r" * 1000) for index in range(80)
    )
    with api(env, AuthScope(42, (WS_A,))) as (client, factory):

        async def save() -> None:
            async with SqlAlchemyRepositoryConventionsUnitOfWork(factory) as uow:
                await uow.conventions.save_and_record_trace(
                    RUN_DONE,
                    CachedConventions(
                        repository_id=REPO_A,
                        agents_md_sha=None,
                        prompt_version_id=PROMPT,
                        key_patterns=("One.", "Two.", "Three."),
                        recommendations=("A.", "B.", "C.", "D.", "E."),
                        languages={"python": 1},
                    ),
                    files,
                )
                await uow.commit()

        asyncio.run(save())
        actions = client.get(f"/api/runs/{RUN_DONE}/actions").json()
        body = client.get(f"/api/runs/{RUN_DONE}/actions/0/response").json()

    assert actions[0]["tool"] == "llm.repo_conventions"
    assert actions[0]["response"] is None
    assert actions[0]["responseRef"] == f"/api/runs/{RUN_DONE}/actions/0/response"
    assert len(body["files"]) == 80


@pytest.mark.integration
def test_run_detail_keeps_the_summary_when_the_review_output_is_truncated(env: Env) -> None:
    raw_summary = {"problem": "A bug.", "done_well": "Small change.", "effort": "small"}
    huge_output = {"findings": [{"body": "z" * (ROW_LIMIT_BYTES + 10)}], "summary": raw_summary}
    finalized_summary = {
        "problem": "Подтверждено замечаний: 1 (correctness: 1).",
        "done_well": "Проанализировано файлов с текстовыми изменениями: 1; исключено файлов: 0.",
        "effort": "small",
    }
    # The seeded run has one published medium finding. A finalized result binds
    # its public summary to that finding set and the run's head, not the model prose.
    finalized = {
        "summary": finalized_summary,
        "verdict": "attention",
        "severity_counts": {"critical": 0, "high": 0, "medium": 1, "low": 0, "info": 0},
        "coverage": RunCoverage(
            CoverageStatus.COMPLETE,
            1,
            head_sha=HEAD,
            diff_digest="d" * 64,
            snapshot_digest="c" * 64,
            total_files=1,
            file_list_complete=True,
        ).to_json(),
    }
    with api(env, AuthScope(42, (WS_A,))) as (client, factory):
        trace = TransactionalRunTrace(partial(SqlAlchemyRunTraceUnitOfWork, factory))
        asyncio.run(trace.record(RUN_DONE, "llm.review_output", {}, huge_output, NOW, 0))
        truncated_detail = client.get(f"/api/runs/{RUN_DONE}").json()
        raw_response = client.get(f"/api/runs/{RUN_DONE}/actions/0/response").json()
        asyncio.run(
            trace.record(RUN_DONE, "review.postprocess", {}, {"summary": raw_summary}, NOW, 0)
        )
        legacy_detail = client.get(f"/api/runs/{RUN_DONE}").json()
        asyncio.run(trace.record(RUN_DONE, "review.postprocess", {}, finalized, NOW, 0))
        detail = client.get(f"/api/runs/{RUN_DONE}").json()

    assert raw_response["truncated"] is True
    assert truncated_detail["summary"] == {
        "problem": (
            "Подтверждено замечаний: 1; Проверка неполная: "
            "нет достоверного сохранённого покрытия (1)."
        ),
        "doneWell": "Проанализировано файлов с текстовыми изменениями: 0; исключено файлов: 0.",
        "effort": "small",
    }
    assert legacy_detail["summary"] == truncated_detail["summary"]
    assert (
        truncated_detail["verdict"] == legacy_detail["verdict"] == detail["verdict"] == "attention"
    )
    assert detail["summary"] == {
        "problem": "Подтверждено замечаний: 1 (correctness: 1).",
        "doneWell": "Проанализировано файлов с текстовыми изменениями: 1; исключено файлов: 0.",
        "effort": "small",
    }
