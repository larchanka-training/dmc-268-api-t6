"""Repository settings and pull requests over REST, scoped by Workspace (#34)."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import text

from app.modules.auth.application.scope import AuthScope
from app.modules.repositories.application.installation_repositories import (
    RepositoryReference,
    RepositorySnapshot,
)
from app.modules.repositories.application.onboard_repository import load_default_rule_sets
from app.modules.repositories.application.sync_installation_repositories import (
    RepositoryOnboardingInput,
    SyncInstallationRepositories,
)
from app.modules.repositories.infrastructure.installation_repository_unit_of_work import (
    SqlAlchemyInstallationRepositoriesUnitOfWork,
)
from app.modules.reviews.application.list_pulls import ListRepositoryPulls
from app.modules.reviews.infrastructure.pull_request_queries import SqlAlchemyPullRequestQueries
from app.modules.workspaces.application.link_github_installations import (
    AuthenticatedGitHubInstallations,
    GitHubInstallation,
    LinkGitHubInstallations,
)
from app.modules.workspaces.infrastructure.github_installation_links import (
    SqlAlchemyGitHubInstallationLinkUnitOfWork,
)
from tests.portal_postgres import (
    INSTALL_A,
    REPO_A,
    REPO_B,
    RULE_A,
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


@pytest.mark.integration
def test_unavailable_repository_reports_effective_disabled_but_keeps_preference_and_grants(
    env: Env,
) -> None:
    with api(env, AuthScope(42, (WS_A,))) as (client, factory):

        async def make_unavailable() -> None:
            async with factory() as session:
                await session.execute(
                    text("UPDATE repositories SET installation_available=false WHERE id=:id"),
                    {"id": REPO_A},
                )
                await session.commit()

        asyncio.run(make_unavailable())
        detail = client.get(f"/api/repos/{REPO_A}")
        assert detail.status_code == 200
        assert detail.json()["enabled"] is False
        assert client.get("/api/repos").json()[0]["enabled"] is False
        assert (
            client.patch(f"/api/repos/{REPO_A}", json={"enabled": False}).json()["enabled"] is False
        )
        resumed = client.patch(f"/api/repos/{REPO_A}", json={"enabled": True})
        assert resumed.status_code == 200
        assert resumed.json()["enabled"] is False
        assert tuple(
            scalar(
                factory,
                "SELECT enabled, installation_available FROM repositories WHERE id=:id",
                id=REPO_A,
            )
        ) == (True, False)
        assert tuple(
            scalar(
                factory,
                "SELECT count(*) FROM github_user_repository_access WHERE github_user_id=42",
            )
        ) == (2,)


@pytest.mark.integration
def test_manual_pause_survives_repeated_repository_onboarding_with_rules_and_grants(
    env: Env,
) -> None:
    with api(env, AuthScope(42, (WS_A,))) as (client, factory):
        paused = client.patch(
            f"/api/repos/{REPO_A}", json={"enabled": False, "waitForCi": "never", "maxComments": 4}
        )
        assert paused.status_code == 200 and paused.json()["enabled"] is False

        async def replay() -> None:
            sync = SyncInstallationRepositories(
                uow_factory=lambda: SqlAlchemyInstallationRepositoriesUnitOfWork(factory),
                rule_sets=load_default_rule_sets(Path("review/rules")),
            )
            for _ in range(2):
                result = await sync.execute(
                    provider_installation_id=INSTALL_A,
                    repositories=(
                        RepositoryOnboardingInput(
                            snapshot=RepositorySnapshot(
                                101, "octo/a", "main", "https://github.test/octo/a"
                            ),
                            languages={"Python": 100},
                        ),
                    ),
                )
                assert result[0].rule_version.repository_id == REPO_A
                assert result[0].created is False

        asyncio.run(replay())
        detail = client.get(f"/api/repos/{REPO_A}")
        assert detail.status_code == 200 and detail.json()["enabled"] is False
        assert client.get("/api/repos").json()[0]["enabled"] is False
        assert tuple(
            scalar(
                factory,
                "SELECT enabled, installation_available, wait_for_ci, max_comments "
                "FROM repositories WHERE id=:id",
                id=REPO_A,
            )
        ) == (False, True, "never", 4)
        assert tuple(
            scalar(
                factory,
                "SELECT id, version FROM rule_versions WHERE repository_id=:id AND is_active",
                id=REPO_A,
            )
        ) == (RULE_A, 1)
        assert tuple(
            scalar(
                factory,
                "SELECT count(*) FROM github_user_repository_access WHERE github_user_id=42",
            )
        ) == (2,)


@pytest.mark.integration
@pytest.mark.parametrize("preference", [True, False])
@pytest.mark.parametrize("whole_installation", [True, False])
def test_app_removal_preserves_preference_and_partial_readd_requires_new_grants(
    env: Env,
    preference: bool,
    whole_installation: bool,
) -> None:
    with api(env, AuthScope(42, (WS_A,))) as (client, factory):
        assert client.patch(f"/api/repos/{REPO_A}", json={"enabled": preference}).status_code == 200
        sync = SyncInstallationRepositories(
            uow_factory=lambda: SqlAlchemyInstallationRepositoriesUnitOfWork(factory),
            rule_sets=load_default_rule_sets(Path("review/rules")),
        )
        asyncio.run(
            sync.disable(
                provider_installation_id=INSTALL_A,
                repositories=(RepositoryReference(101, "octo/a", None, None),),
                delivery_id="remove-once",
                all_repositories=whole_installation,
            )
        )
        assert client.get(f"/api/repos/{REPO_A}").status_code == 404
        assert client.patch(f"/api/repos/{REPO_A}", json={"enabled": True}).status_code == 404
        assert tuple(
            scalar(
                factory,
                "SELECT enabled, installation_available FROM repositories WHERE id=:id",
                id=REPO_A,
            )
        ) == (preference, False)
        assert tuple(
            scalar(
                factory,
                "SELECT enabled, installation_available FROM repositories WHERE id=:id",
                id=REPO_B,
            )
        ) == (True, True)

        if whole_installation:
            return  # A replacement installation is tested in the later rebind slice.

        asyncio.run(
            sync.execute(
                provider_installation_id=INSTALL_A,
                repositories=(
                    RepositoryOnboardingInput(
                        snapshot=RepositorySnapshot(
                            101, "octo/a", "main", "https://github.test/octo/a"
                        ),
                        languages={"Python": 100},
                    ),
                ),
            )
        )
        assert tuple(
            scalar(
                factory,
                "SELECT enabled, installation_available FROM repositories WHERE id=:id",
                id=REPO_A,
            )
        ) == (preference, True)
        assert client.get(f"/api/repos/{REPO_A}").status_code == 404
        assert client.get("/api/repos").json() == []
        assert tuple(
            scalar(
                factory,
                "SELECT id, version FROM rule_versions WHERE repository_id=:id AND is_active",
                id=REPO_A,
            )
        ) == (RULE_A, 1)

        class GitHub:
            async def identify_user(self, access_token: str) -> int:
                return 42

            async def list_for_user(self, access_token: str) -> AuthenticatedGitHubInstallations:
                return AuthenticatedGitHubInstallations(
                    42, (GitHubInstallation(17, "octo", (101,), account_external_id=700),)
                )

        asyncio.run(
            LinkGitHubInstallations(
                github=GitHub(),
                uow_factory=lambda: SqlAlchemyGitHubInstallationLinkUnitOfWork(factory),
            ).execute("fresh-user-token")
        )
        restored = client.get(f"/api/repos/{REPO_A}")
        assert restored.status_code == 200 and restored.json()["enabled"] is preference
