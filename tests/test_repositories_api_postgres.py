"""Repository settings and pull requests over REST, scoped by Workspace (#34)."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Iterator

import pytest

from app.modules.auth.application.scope import AuthScope
from app.modules.reviews.application.list_pulls import ListRepositoryPulls
from app.modules.reviews.infrastructure.pull_request_queries import SqlAlchemyPullRequestQueries
from tests.portal_postgres import (
    REPO_A,
    REPO_B,
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
def test_repositories_and_pulls_are_scoped_to_the_claimed_workspace(env: Env) -> None:
    with api(env, AuthScope(42, (WS_A,))) as (client, factory):
        repos = client.get("/api/repos").json()
        other = client.get(f"/api/repos/{REPO_B}")
        patched = client.patch(
            f"/api/repos/{REPO_A}", json={"waitForCi": "never", "maxComments": 4}
        )
        patch_other = client.patch(f"/api/repos/{REPO_B}", json={"enabled": False})
        pulls = client.get(f"/api/repos/{REPO_A}/pulls").json()
        closed = client.get(f"/api/repos/{REPO_A}/pulls", params={"state": "closed"}).json()
        pulls_other = client.get(f"/api/repos/{REPO_B}/pulls")
        stored = scalar(
            factory, "SELECT wait_for_ci, max_comments FROM repositories WHERE id = :id", id=REPO_A
        )
        untouched = scalar(factory, "SELECT enabled FROM repositories WHERE id = :id", id=REPO_B)

        async def paged() -> list[list[int]]:
            use_case = ListRepositoryPulls(
                SqlAlchemyPullRequestQueries(factory, AuthScope(42, (WS_A,)))
            )
            first = await use_case.execute(REPO_A, state="all", limit=2)
            assert first is not None and first.next_cursor is not None
            second = await use_case.execute(REPO_A, state="all", cursor=first.next_cursor, limit=2)
            assert second is not None and second.next_cursor is None
            return [[item.number for item in page.items] for page in (first, second)]

        pages = asyncio.run(paged())

    assert [item["id"] for item in repos] == [str(REPO_A)]
    assert repos[0]["reviewEvent"] == "COMMENT"
    assert other.status_code == 404 and patch_other.status_code == 404
    assert patched.status_code == 200
    assert tuple(stored) == ("never", 4) and tuple(untouched) == (True,)
    assert [item["number"] for item in pulls["items"]] == [3, 1]
    assert pulls["items"][1]["latestRun"] == {
        "id": str(RUN_DONE),
        "status": "succeeded",
        "verdict": "attention",
    }
    assert pulls["items"][1]["author"] == "octocat"
    assert [item["number"] for item in closed["items"]] == [2]
    assert pulls_other.status_code == 404
    assert pages == [[3, 2], [1]]
