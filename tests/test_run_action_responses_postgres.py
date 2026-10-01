"""Run action responses over 64 KB stored by reference (#34, D1)."""

from __future__ import annotations

import asyncio
import json
from functools import partial

import pytest

from app.modules.auth.application.scope import AuthScope
from app.modules.reviews.application.conventions import CachedConventions, ConventionsFile
from app.modules.reviews.application.run_trace import TransactionalRunTrace
from app.modules.reviews.infrastructure.conventions_unit_of_work import (
    SqlAlchemyRepositoryConventionsUnitOfWork,
)
from app.modules.reviews.infrastructure.run_action_payloads import ROW_LIMIT_BYTES
from app.modules.reviews.infrastructure.run_lifecycle_store import (
    SqlAlchemyRunTraceUnitOfWork,
)
from tests.portal_postgres import (
    NOW,
    PROMPT,
    REPO_A,
    RUN_DONE,
    WS_A,
    Env,
    api,
    rest_env,  # noqa: F401  (the env fixture)
    scalar,
)


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
    summary = {"problem": "A bug.", "done_well": "Small change.", "effort": "small"}
    huge_output = {"findings": [{"body": "z" * (ROW_LIMIT_BYTES + 10)}], "summary": summary}
    with api(env, AuthScope(42, (WS_A,))) as (client, factory):
        trace = TransactionalRunTrace(partial(SqlAlchemyRunTraceUnitOfWork, factory))
        asyncio.run(trace.record(RUN_DONE, "llm.review_output", {}, huge_output, NOW, 0))
        truncated_detail = client.get(f"/api/runs/{RUN_DONE}").json()
        asyncio.run(trace.record(RUN_DONE, "review.postprocess", {}, {"summary": summary}, NOW, 0))
        detail = client.get(f"/api/runs/{RUN_DONE}").json()

    assert truncated_detail["summary"] is None
    assert detail["summary"] == {
        "problem": "A bug.",
        "doneWell": "Small change.",
        "effort": "small",
    }
