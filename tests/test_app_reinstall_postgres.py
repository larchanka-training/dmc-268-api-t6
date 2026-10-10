"""Fresh OAuth preservation and conservative account-lineage boundaries on real PG."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Iterator
from dataclasses import dataclass
from decimal import Decimal
from uuid import UUID

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.modules.auth.application.scope import AuthScope
from app.modules.repositories.application.installation_repositories import RepositorySnapshot
from app.modules.repositories.infrastructure.installation_repository_unit_of_work import (
    SqlAlchemyInstallationRepositoryStore,
)
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
    PROMPT,
    REPO_A,
    RULE_A,
    RUN_DONE,
    WS_A,
    WS_B,
    Env,
    api,
    portal_schema,
    scalar,
)

_CONVENTIONS = UUID(int=11301)
_ACTION = UUID(int=11302)


@pytest.fixture
def reinstall_env() -> Iterator[Env]:
    database_url = os.environ.get("TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("set TEST_DATABASE_URL to run PostgreSQL integration tests")
    with portal_schema(database_url, None) as env:
        yield env


@dataclass
class FreshGitHub:
    user_id: int = 42
    installation: GitHubInstallation = GitHubInstallation(29, "renamed-owner", (101,), 700)

    async def identify_user(self, access_token: str) -> int:
        return self.user_id

    async def list_for_user(self, access_token: str) -> AuthenticatedGitHubInstallations:
        return AuthenticatedGitHubInstallations(self.user_id, (self.installation,))


async def seed_archive(factory: async_sessionmaker[AsyncSession]) -> None:
    async with factory.begin() as session:
        await session.execute(
            text(
                "UPDATE provider_installations SET account_external_id=700, "
                "deleted_at=clock_timestamp()-interval '1 day' WHERE id=:id"
            ),
            {"id": INSTALL_A},
        )
        await session.execute(
            text(
                "UPDATE repositories SET enabled=false, installation_available=false, "
                "default_engine='fast', wait_for_ci='always', max_comments=7, "
                "review_event='request_changes', "
                "prompt_version_id=:prompt WHERE id=:repo"
            ),
            {"repo": REPO_A, "prompt": PROMPT},
        )
        await session.execute(
            text("UPDATE workspaces SET daily_budget_usd=12.34 WHERE id=:ws"), {"ws": WS_A}
        )
        await session.execute(text("UPDATE runs SET state='succeeded' WHERE state='queued'"))
        await session.execute(
            text(
                "INSERT INTO github_installation_access_revocations "
                "(provider_installation_id, repository_external_id, revoked_at) "
                "VALUES (:id, 0, clock_timestamp()-interval '1 day')"
            ),
            {"id": INSTALL_A},
        )
        await session.execute(
            text("DELETE FROM github_user_repository_access WHERE provider_installation_id=:id"),
            {"id": INSTALL_A},
        )
        await session.execute(text("INSERT INTO github_user_profiles(id,login) VALUES (43,'bob')"))
        await session.execute(
            text(
                "INSERT INTO github_user_workspace_access(github_user_id,workspace_id) "
                "VALUES (43,:ws)"
            ),
            {"ws": WS_A},
        )
        await session.execute(
            text(
                "INSERT INTO repo_conventions (id, repository_id, prompt_version_id, agents_md, "
                "key_patterns, recommendations, languages) "
                "VALUES (:id,:repo,:prompt,'custom rules', "
                "'[\"*.py\"]','[\"preserve me\"]','{\"Python\": 10}')"
            ),
            {"id": _CONVENTIONS, "repo": REPO_A, "prompt": PROMPT},
        )
        await session.execute(
            text(
                'INSERT INTO run_actions (id,run_id,"index",tool,request,started_at,duration_ms) '
                "VALUES (:id,:run,0,'fetch_diff','{}',clock_timestamp(),25)"
            ),
            {"id": _ACTION, "run": RUN_DONE},
        )


async def apply(factory: async_sessionmaker[AsyncSession], github: FreshGitHub) -> tuple[UUID, ...]:
    return await LinkGitHubInstallations(
        github=github,
        uow_factory=lambda: SqlAlchemyGitHubInstallationLinkUnitOfWork(factory),
    ).execute("fresh-user-token")


@pytest.mark.integration
def test_same_account_fresh_oauth_preserves_repository_children_workspace_and_pause(
    reinstall_env: Env,
) -> None:
    with api(reinstall_env, AuthScope(42, (WS_A,))) as (client, factory):
        asyncio.run(seed_archive(factory))
        assert client.get(f"/api/repos/{REPO_A}").status_code == 404
        assert client.get(f"/api/runs/{RUN_DONE}").status_code == 404
        assert asyncio.run(apply(factory, FreshGitHub())) == (WS_A,)
        assert client.get(f"/api/repos/{REPO_A}").status_code == 200
        assert client.get(f"/api/repos/{REPO_A}").json()["enabled"] is False
        assert client.get(f"/api/runs/{RUN_DONE}").status_code == 200
        assert tuple(
            scalar(
                factory,
                "SELECT i.external_id, i.account_external_id, i.workspace_id, r.enabled, "
                "r.installation_available, r.default_engine, r.wait_for_ci, r.max_comments, "
                "r.review_event, r.prompt_version_id, w.daily_budget_usd FROM repositories r "
                "JOIN provider_installations i ON i.id=r.provider_installation_id "
                "JOIN workspaces w ON w.id=i.workspace_id WHERE r.id=:repo",
                repo=REPO_A,
            )
        ) == (
            29,
            700,
            WS_A,
            False,
            True,
            "fast",
            "always",
            7,
            "request_changes",
            PROMPT,
            Decimal("12.34"),
        )
        assert tuple(
            scalar(
                factory,
                "SELECT external_id, account_external_id, deleted_at IS NOT NULL FROM "
                "provider_installations WHERE id=:id",
                id=INSTALL_A,
            )
        ) == (17, 700, True)
        assert tuple(
            scalar(
                factory,
                "SELECT count(*) FROM code_changes WHERE repository_id=:repo",
                repo=REPO_A,
            )
        ) == (3,)
        assert tuple(scalar(factory, "SELECT count(*) FROM runs")) == (4,)
        assert tuple(
            scalar(
                factory,
                "SELECT repository_id, is_active FROM rule_versions WHERE id=:id",
                id=RULE_A,
            )
        ) == (REPO_A, True)
        assert tuple(
            scalar(
                factory,
                "SELECT repository_id, agents_md FROM repo_conventions WHERE id=:id",
                id=_CONVENTIONS,
            )
        ) == (REPO_A, "custom rules")
        assert tuple(
            scalar(factory, "SELECT run_id, tool FROM run_actions WHERE id=:id", id=_ACTION)
        ) == (RUN_DONE, "fetch_diff")
        assert tuple(
            scalar(factory, "SELECT run_id, body FROM findings WHERE id=:id", id=UUID(int=77))
        ) == (RUN_DONE, "B")
        assert tuple(
            scalar(
                factory,
                "SELECT count(*) FROM github_user_repository_access WHERE github_user_id=43",
            )
        ) == (0,)
    with api(reinstall_env, AuthScope(43, (WS_A,))) as (client, _):
        assert client.get(f"/api/repos/{REPO_A}").status_code == 404
        assert client.get(f"/api/runs/{RUN_DONE}").status_code == 404


@pytest.mark.integration
@pytest.mark.parametrize(
    "variant",
    [
        "other-account",
        "legacy",
        "missing-snapshot",
        "ambiguous-workspaces",
        "ambiguous-repo",
        "source-active",
    ],
)
def test_unverified_or_ambiguous_lineage_keeps_archive_and_onboards_clean_target(
    reinstall_env: Env,
    variant: str,
) -> None:
    async def exercise() -> tuple[UUID, UUID]:
        engine = reinstall_env.engine()
        factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            await seed_archive(factory)
            account: int | None = 701 if variant == "other-account" else 700
            async with factory.begin() as session:
                if variant == "legacy":
                    await session.execute(
                        text(
                            "UPDATE provider_installations SET account_external_id=NULL WHERE "
                            "id=:id"
                        ),
                        {"id": INSTALL_A},
                    )
                if variant == "source-active":
                    await session.execute(
                        text("UPDATE provider_installations SET deleted_at=NULL WHERE id=:id"),
                        {"id": INSTALL_A},
                    )
                if variant == "missing-snapshot":
                    account = None
                    await session.execute(
                        text(
                            "INSERT INTO "
                            "provider_installations(id,workspace_id,provider,external_id,account_external_id,metadata)"
                            " VALUES (:id,:ws,'github',29,700,'{}')"
                        ),
                        {"id": UUID(int=11303), "ws": WS_A},
                    )
                if variant.startswith("ambiguous"):
                    duplicate_workspace = (
                        UUID(int=11304) if variant == "ambiguous-workspaces" else WS_A
                    )
                    if variant == "ambiguous-workspaces":
                        await session.execute(
                            text(
                                "INSERT INTO workspaces(id,name,daily_budget_usd) VALUES "
                                "(:ws,'archive',9)"
                            ),
                            {"ws": duplicate_workspace},
                        )
                    await session.execute(
                        text(
                            "INSERT INTO "
                            "provider_installations(id,workspace_id,provider,external_id,account_external_id,deleted_at,metadata)"
                            " VALUES (:id,:ws,'github',19,700,clock_timestamp(),'{}')"
                        ),
                        {"id": UUID(int=11305), "ws": duplicate_workspace},
                    )
                    await session.execute(
                        text(
                            "INSERT INTO "
                            "repositories(id,provider_installation_id,external_id,full_name,default_branch,web_url,installation_available)"
                            " VALUES "
                            "(:id,:installation,101,'octo/a','main','https://github.test/octo/a',false)"
                        ),
                        {"id": UUID(int=11306), "installation": UUID(int=11305)},
                    )
            workspace_ids = await apply(
                factory, FreshGitHub(installation=GitHubInstallation(29, "octo", (101,), account))
            )
            target_workspace = workspace_ids[0]
            if variant != "missing-snapshot":
                assert target_workspace != WS_A
            async with factory.begin() as session:
                target_id = await session.scalar(
                    text(
                        "SELECT id FROM provider_installations WHERE external_id=29 AND "
                        "provider='github'"
                    )
                )
                assert isinstance(target_id, UUID)
                repo_id = await SqlAlchemyInstallationRepositoryStore(session).upsert_repository(
                    target_id,
                    RepositorySnapshot(
                        101, "new-owner/a", "main", "https://github.test/new-owner/a"
                    ),
                )
            assert repo_id != REPO_A
            async with factory() as session:
                old = (
                    await session.execute(
                        text(
                            "SELECT "
                            "provider_installation_id,enabled,installation_available,max_comments "
                            "FROM repositories WHERE id=:id"
                        ),
                        {"id": REPO_A},
                    )
                ).one()
                assert tuple(old) == (INSTALL_A, False, False, 7)
                new = (
                    await session.execute(
                        text(
                            "SELECT enabled,installation_available,max_comments,prompt_version_id "
                            "FROM repositories WHERE id=:id"
                        ),
                        {"id": repo_id},
                    )
                ).one()
                assert tuple(new) == (True, True, 10, None)
                assert (
                    await session.scalar(
                        text("SELECT count(*) FROM code_changes WHERE repository_id=:id"),
                        {"id": repo_id},
                    )
                    == 0
                )
                assert (
                    await session.scalar(
                        text(
                            "SELECT count(*) FROM github_user_repository_access WHERE "
                            "github_user_id=43"
                        )
                    )
                    == 0
                )
            return target_workspace, repo_id
        finally:
            await engine.dispose()

    workspace, target_repo = asyncio.run(exercise())
    with api(reinstall_env, AuthScope(42, (WS_A, workspace))) as (client, _):
        assert client.get(f"/api/repos/{REPO_A}").status_code == 404
        assert client.get(f"/api/runs/{RUN_DONE}").status_code == 404
        assert client.get(f"/api/repos/{target_repo}").status_code == 200


@pytest.mark.integration
def test_oauth_moves_only_confirmed_repository_and_preserves_deep_setting(
    reinstall_env: Env,
) -> None:
    async def exercise() -> None:
        engine = reinstall_env.engine()
        factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            await seed_archive(factory)
            async with factory.begin() as session:
                await session.execute(
                    text("UPDATE repositories SET default_engine='deep' WHERE id=:repo"),
                    {"repo": REPO_A},
                )
                await session.execute(
                    text(
                        "INSERT INTO "
                        "repositories(id,provider_installation_id,external_id,full_name,default_branch,web_url,enabled,installation_available)"
                        " VALUES "
                        "(:id,:installation,103,'octo/unlisted','main','https://github.test/octo/unlisted',false,false)"
                    ),
                    {"id": UUID(int=11307), "installation": INSTALL_A},
                )
            assert await apply(factory, FreshGitHub()) == (WS_A,)
            async with factory() as session:
                assert (
                    await session.scalar(
                        text("SELECT default_engine FROM repositories WHERE id=:repo"),
                        {"repo": REPO_A},
                    )
                    == "deep"
                )
                row = (
                    await session.execute(
                        text(
                            "SELECT provider_installation_id,enabled,installation_available FROM "
                            "repositories WHERE id=:id"
                        ),
                        {"id": UUID(int=11307)},
                    )
                ).one()
                assert tuple(row) == (INSTALL_A, False, False)
                assert (
                    await session.scalar(
                        text(
                            "SELECT count(*) FROM github_user_repository_access WHERE "
                            "repository_external_id=103"
                        )
                    )
                    == 0
                )
        finally:
            await engine.dispose()

    asyncio.run(exercise())


@pytest.mark.integration
@pytest.mark.parametrize("same_user", [True, False])
def test_concurrent_fresh_callbacks_share_one_target_and_preserved_repository(
    reinstall_env: Env,
    same_user: bool,
) -> None:
    async def exercise() -> None:
        engine = reinstall_env.engine()
        factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            await seed_archive(factory)
            results = await asyncio.wait_for(
                asyncio.gather(
                    apply(factory, FreshGitHub()),
                    apply(factory, FreshGitHub(user_id=42 if same_user else 43)),
                ),
                timeout=10,
            )
            assert tuple(results) == ((WS_A,), (WS_A,))
            async with factory() as session:
                assert (
                    await session.scalar(
                        text(
                            "SELECT count(*) FROM provider_installations WHERE provider='github' "
                            "AND external_id=29"
                        )
                    )
                    == 1
                )
                assert (
                    await session.scalar(
                        text("SELECT count(*) FROM repositories WHERE external_id=101")
                    )
                    == 1
                )
                owners = tuple(
                    (
                        await session.scalars(
                            text(
                                "SELECT github_user_id FROM github_user_repository_access WHERE "
                                "repository_external_id=101 ORDER BY github_user_id"
                            )
                        )
                    ).all()
                )
                assert owners == ((42,) if same_user else (42, 43))
                assert await session.scalar(text("SELECT count(*) FROM runs")) == 4
        finally:
            await engine.dispose()

    asyncio.run(exercise())


@pytest.mark.integration
@pytest.mark.parametrize("whole_removal", [False, True])
def test_target_removal_after_snapshot_reservation_prevents_rebind_and_new_grants(
    reinstall_env: Env,
    whole_removal: bool,
) -> None:
    async def exercise() -> None:
        engine = reinstall_env.engine()
        factory = async_sessionmaker(engine, expire_on_commit=False)
        started = asyncio.Event()
        release = asyncio.Event()

        class DelayedGitHub(FreshGitHub):
            async def list_for_user(self, access_token: str) -> AuthenticatedGitHubInstallations:
                started.set()
                await release.wait()
                return await super().list_for_user(access_token)

        try:
            await seed_archive(factory)
            target_id = UUID(int=11308)
            async with factory.begin() as session:
                await session.execute(
                    text(
                        "INSERT INTO "
                        "provider_installations(id,workspace_id,provider,external_id,account_external_id,metadata)"
                        " VALUES (:id,:ws,'github',29,700,'{}')"
                    ),
                    {"id": target_id, "ws": WS_A},
                )
            oauth = asyncio.create_task(apply(factory, DelayedGitHub()))
            await asyncio.wait_for(started.wait(), timeout=2)
            async with factory.begin() as session:
                store = SqlAlchemyInstallationRepositoryStore(session)
                if whole_removal:
                    await store.disable_installation(target_id)
                else:
                    await store.disable_repository(target_id, 101)
            release.set()
            assert await asyncio.wait_for(oauth, timeout=5) == (WS_A,)
            async with factory() as session:
                assert (
                    await session.scalar(
                        text("SELECT provider_installation_id FROM repositories WHERE id=:repo"),
                        {"repo": REPO_A},
                    )
                    == INSTALL_A
                )
                assert (
                    await session.scalar(
                        text(
                            "SELECT count(*) FROM github_user_repository_access WHERE "
                            "provider_installation_id=:id"
                        ),
                        {"id": target_id},
                    )
                    == 0
                )
                assert await session.scalar(text("SELECT count(*) FROM runs")) == 4
        finally:
            release.set()
            await engine.dispose()

    asyncio.run(exercise())


@pytest.mark.integration
def test_deleted_legacy_installation_cannot_gain_source_identity_from_late_oauth(
    reinstall_env: Env,
) -> None:
    async def exercise() -> None:
        engine = reinstall_env.engine()
        factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            await seed_archive(factory)
            async with factory.begin() as session:
                await session.execute(
                    text("UPDATE provider_installations SET account_external_id=NULL WHERE id=:id"),
                    {"id": INSTALL_A},
                )
            await apply(
                factory, FreshGitHub(installation=GitHubInstallation(17, "octo", (101,), 700))
            )
            async with factory() as session:
                assert (
                    await session.scalar(
                        text("SELECT account_external_id FROM provider_installations WHERE id=:id"),
                        {"id": INSTALL_A},
                    )
                    is None
                )
                assert (
                    await session.scalar(
                        text(
                            "SELECT count(*) FROM github_user_repository_access WHERE "
                            "provider_installation_id=:id"
                        ),
                        {"id": INSTALL_A},
                    )
                    == 0
                )
            assert await apply(factory, FreshGitHub()) != (WS_A,)
            async with factory() as session:
                assert (
                    await session.scalar(
                        text("SELECT provider_installation_id FROM repositories WHERE id=:repo"),
                        {"repo": REPO_A},
                    )
                    == INSTALL_A
                )
        finally:
            await engine.dispose()

    asyncio.run(exercise())


@pytest.mark.integration
@pytest.mark.parametrize("different_workspace", [False, True])
def test_existing_target_repository_is_not_replaced_or_merged_with_archive(
    reinstall_env: Env,
    different_workspace: bool,
) -> None:
    target_workspace = WS_B if different_workspace else WS_A
    target_repo = UUID(int=11309)

    async def exercise() -> None:
        engine = reinstall_env.engine()
        factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            await seed_archive(factory)
            async with factory.begin() as session:
                await session.execute(
                    text(
                        "INSERT INTO "
                        "provider_installations(id,workspace_id,provider,external_id,account_external_id,metadata)"
                        " VALUES (:id,:ws,'github',29,700,'{}')"
                    ),
                    {"id": UUID(int=11310), "ws": target_workspace},
                )
                await session.execute(
                    text(
                        "INSERT INTO "
                        "repositories(id,provider_installation_id,external_id,full_name,default_branch,web_url,max_comments)"
                        " VALUES "
                        "(:id,:installation,101,'new-owner/a','main','https://github.test/new-owner/a',2)"
                    ),
                    {"id": target_repo, "installation": UUID(int=11310)},
                )
            assert await apply(factory, FreshGitHub()) == (target_workspace,)
            async with factory() as session:
                assert (
                    await session.scalar(
                        text("SELECT provider_installation_id FROM repositories WHERE id=:id"),
                        {"id": REPO_A},
                    )
                    == INSTALL_A
                )
                assert (
                    await session.scalar(
                        text("SELECT max_comments FROM repositories WHERE id=:id"),
                        {"id": target_repo},
                    )
                    == 2
                )
                assert (
                    await session.scalar(
                        text("SELECT count(*) FROM code_changes WHERE repository_id=:id"),
                        {"id": target_repo},
                    )
                    == 0
                )
                assert (
                    await session.scalar(
                        text("SELECT count(*) FROM repositories WHERE external_id=101")
                    )
                    == 2
                )
        finally:
            await engine.dispose()

    asyncio.run(exercise())
    with api(reinstall_env, AuthScope(42, (WS_A, target_workspace))) as (client, _):
        assert client.get(f"/api/repos/{REPO_A}").status_code == 404
        assert client.get(f"/api/runs/{RUN_DONE}").status_code == 404
        assert client.get(f"/api/repos/{target_repo}").status_code == 200


@pytest.mark.integration
def test_empty_second_deleted_workspace_makes_account_lineage_ambiguous(reinstall_env: Env) -> None:
    async def exercise() -> None:
        engine = reinstall_env.engine()
        factory = async_sessionmaker(engine, expire_on_commit=False)
        empty_workspace = UUID(int=11311)
        try:
            await seed_archive(factory)
            async with factory.begin() as session:
                await session.execute(
                    text(
                        "INSERT INTO workspaces(id,name,daily_budget_usd) VALUES (:ws,'empty "
                        "archive',5.67)"
                    ),
                    {"ws": empty_workspace},
                )
                await session.execute(
                    text(
                        "INSERT INTO provider_installations "
                        "(id,workspace_id,provider,external_id,account_external_id,deleted_at,metadata)"
                        " "
                        "VALUES (:id,:ws,'github',19,700,clock_timestamp(),'{}')"
                    ),
                    {"id": UUID(int=11312), "ws": empty_workspace},
                )
            target_workspace = (await apply(factory, FreshGitHub()))[0]
            assert target_workspace not in (WS_A, empty_workspace)
            async with factory() as session:
                assert (
                    await session.scalar(
                        text("SELECT provider_installation_id FROM repositories WHERE id=:repo"),
                        {"repo": REPO_A},
                    )
                    == INSTALL_A
                )
                assert (
                    await session.scalar(
                        text(
                            "SELECT count(*) FROM github_user_repository_access WHERE "
                            "provider_installation_id=:id"
                        ),
                        {"id": INSTALL_A},
                    )
                    == 0
                )
        finally:
            await engine.dispose()

    asyncio.run(exercise())


@pytest.mark.integration
def test_unique_empty_deleted_workspace_preserves_budget_with_new_repository_identity(
    reinstall_env: Env,
) -> None:
    async def exercise() -> None:
        engine = reinstall_env.engine()
        factory = async_sessionmaker(engine, expire_on_commit=False)
        empty_workspace = UUID(int=11313)
        try:
            async with factory.begin() as session:
                await session.execute(
                    text(
                        "INSERT INTO workspaces(id,name,daily_budget_usd) VALUES (:ws,'empty "
                        "archive',5.67)"
                    ),
                    {"ws": empty_workspace},
                )
                await session.execute(
                    text(
                        "INSERT INTO provider_installations "
                        "(id,workspace_id,provider,external_id,account_external_id,deleted_at,metadata)"
                        " "
                        "VALUES (:id,:ws,'github',77,800,clock_timestamp(),'{}')"
                    ),
                    {"id": UUID(int=11314), "ws": empty_workspace},
                )
            github = FreshGitHub(installation=GitHubInstallation(88, "same-owner", (104,), 800))
            assert await apply(factory, github) == (empty_workspace,)
            async with factory.begin() as session:
                target_id = await session.scalar(
                    text(
                        "SELECT id FROM provider_installations WHERE provider='github' AND "
                        "external_id=88"
                    )
                )
                assert isinstance(target_id, UUID)
                new_repo = await SqlAlchemyInstallationRepositoryStore(session).upsert_repository(
                    target_id,
                    RepositorySnapshot(
                        104, "same-owner/new", "main", "https://github.test/same-owner/new"
                    ),
                )
            async with factory() as session:
                assert await session.scalar(
                    text("SELECT daily_budget_usd FROM workspaces WHERE id=:ws"),
                    {"ws": empty_workspace},
                ) == Decimal("5.67")
                row = (
                    await session.execute(
                        text(
                            "SELECT enabled,installation_available,max_comments,prompt_version_id "
                            "FROM repositories WHERE id=:id"
                        ),
                        {"id": new_repo},
                    )
                ).one()
                assert tuple(row) == (True, True, 10, None)
                assert new_repo != REPO_A
                assert (
                    await session.scalar(
                        text("SELECT count(*) FROM code_changes WHERE repository_id=:id"),
                        {"id": new_repo},
                    )
                    == 0
                )
                assert (
                    await session.scalar(
                        text("SELECT external_id FROM provider_installations WHERE id=:id"),
                        {"id": UUID(int=11314)},
                    )
                    == 77
                )
        finally:
            await engine.dispose()

    asyncio.run(exercise())
