"""Fresh OAuth preservation and conservative account-lineage boundaries on real PG."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Iterator
from dataclasses import dataclass
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.modules.auth.application.scope import AuthScope
from app.modules.repositories.application.installation_repositories import (
    RepositoryReference,
    RepositorySnapshot,
)
from app.modules.repositories.application.onboard_repository import (
    OnboardingResult,
    load_default_rule_sets,
)
from app.modules.repositories.application.sync_installation_repositories import (
    RepositoryOnboardingInput,
    SyncInstallationRepositories,
)
from app.modules.repositories.infrastructure.installation_repository_unit_of_work import (
    SqlAlchemyInstallationRepositoriesUnitOfWork,
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
        # Three non-default #149/#145 settings below: inherited by other files using seed_archive.
        await session.execute(
            text(
                "UPDATE repositories SET enabled=false, installation_available=false, "
                "default_engine='fast', wait_for_ci='always', max_comments=7, "
                "review_event='request_changes', branch_filter='{main,release/*}', "
                "severity_threshold='critical', rag_enabled=true, "
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
        repository = client.get(f"/api/repos/{REPO_A}").json()
        assert repository["branchFilter"] == ["main", "release/*"]
        assert repository["severityThreshold"] == "critical" and repository["ragEnabled"] is True
        assert client.get(f"/api/runs/{RUN_DONE}").status_code == 200
        assert tuple(
            scalar(
                factory,
                "SELECT i.external_id, i.account_external_id, i.workspace_id, r.enabled, "
                "r.installation_available, r.default_engine, r.wait_for_ci, r.max_comments, "
                "r.review_event, r.prompt_version_id, r.branch_filter, r.severity_threshold, "
                "r.rag_enabled, w.daily_budget_usd FROM repositories r "
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
            ["main", "release/*"],
            "critical",
            True,
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
            assert isinstance(repo_id, UUID)
            assert repo_id != REPO_A
            async with factory() as session:
                old = (
                    await session.execute(
                        text(
                            "SELECT "
                            "provider_installation_id,enabled,installation_available,"
                            "max_comments "
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
                    text(
                        "UPDATE repositories SET default_engine='deep',rag_enabled=true "
                        "WHERE id=:repo"
                    ),
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
                assert (
                    await session.scalar(
                        text("SELECT rag_enabled FROM repositories WHERE id=:repo"),
                        {"repo": REPO_A},
                    )
                    is True
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
def test_rebind_keeps_other_account_deleted_installation_out_of_same_workspace_sources(
    reinstall_env: Env,
) -> None:
    async def exercise() -> None:
        engine = reinstall_env.engine()
        factory = async_sessionmaker(engine, expire_on_commit=False)
        other_installation = UUID(int=11315)
        other_repo = UUID(int=11316)
        try:
            await seed_archive(factory)
            async with factory.begin() as session:
                # Same Workspace, other account: archived_workspace never locks it, so only
                # the account predicate of the rebind() source query keeps 105 in place.
                await session.execute(
                    text(
                        "INSERT INTO "
                        "provider_installations(id,workspace_id,provider,external_id,account_external_id,deleted_at,metadata)"
                        " VALUES (:id,:ws,'github',19,701,clock_timestamp(),'{}')"
                    ),
                    {"id": other_installation, "ws": WS_A},
                )
                await session.execute(
                    text(
                        "INSERT INTO "
                        "repositories(id,provider_installation_id,external_id,full_name,default_branch,web_url,installation_available)"
                        " VALUES "
                        "(:id,:installation,105,'other/b','main','https://github.test/other/b',false)"
                    ),
                    {"id": other_repo, "installation": other_installation},
                )
            assert await apply(
                factory,
                FreshGitHub(installation=GitHubInstallation(29, "renamed-owner", (101, 105), 700)),
            ) == (WS_A,)
            async with factory() as session:
                target = await session.scalar(
                    text("SELECT id FROM provider_installations WHERE external_id=29")
                )
                assert isinstance(target, UUID)
                rows = (
                    await session.execute(
                        text(
                            "SELECT id, provider_installation_id, installation_available FROM "
                            "repositories WHERE external_id IN (101, 105) ORDER BY external_id"
                        )
                    )
                ).all()
                assert [tuple(row) for row in rows] == [
                    (REPO_A, target, True),
                    (other_repo, other_installation, False),
                ]
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
            select_rows = text(
                "SELECT id, to_jsonb(r) FROM repositories r WHERE id IN (:archive, :target)"
            )
            ids = {"archive": REPO_A, "target": target_repo}
            async with factory() as session:
                before = {row[0]: row[1] for row in await session.execute(select_rows, ids)}
            # [True] returns early at rebinding L77-78; only [False] reaches the loop guard.
            assert await apply(factory, FreshGitHub()) == (target_workspace,)
            async with factory() as session:
                # Whole rows, every column: neither side is replaced, merged or reset.
                after = {row[0]: row[1] for row in await session.execute(select_rows, ids)}
                assert after == before
                assert {
                    key: after[REPO_A][key]
                    for key in (
                        "provider_installation_id",
                        "enabled",
                        "installation_available",
                        "branch_filter",
                        "severity_threshold",
                        "rag_enabled",
                    )
                } == {
                    "provider_installation_id": str(INSTALL_A),
                    "enabled": False,
                    "installation_available": False,
                    "branch_filter": ["main", "release/*"],
                    "severity_threshold": "critical",
                    "rag_enabled": True,
                }
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
def test_existing_clean_target_recovers_after_null_account_oauth_remove_and_add(
    reinstall_env: Env,
) -> None:
    """Fresh NULL-account consent can create a clean row beside a same-Workspace archive."""

    async def exercise() -> None:
        engine = reinstall_env.engine()
        factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            await seed_archive(factory)
            # Link the target to the verified account without confirming archived 101.
            assert await apply(
                factory, FreshGitHub(installation=GitHubInstallation(29, "org", (), 700))
            ) == (WS_A,)
            async with factory() as session:
                target = await session.scalar(
                    text("SELECT id FROM provider_installations WHERE external_id=29")
                )
                assert isinstance(target, UUID)
            # Another user's snapshot lacks numeric identity: consent grants access,
            # but cannot prove archive ownership or move the archived Repository.
            assert await apply(
                factory,
                FreshGitHub(user_id=43, installation=GitHubInstallation(29, "org", (101,), None)),
            ) == (WS_A,)
            async with factory() as session:
                assert (
                    await session.scalar(
                        text(
                            "SELECT count(*) FROM github_user_repository_access "
                            "WHERE github_user_id=43 AND provider_installation_id=:id "
                            "AND repository_external_id=101"
                        ),
                        {"id": target},
                    )
                    == 1
                )
                assert (
                    await session.scalar(
                        text("SELECT provider_installation_id FROM repositories WHERE id=:id"),
                        {"id": REPO_A},
                    )
                    == INSTALL_A
                )
            sync = SyncInstallationRepositories(
                uow_factory=lambda: SqlAlchemyInstallationRepositoriesUnitOfWork(factory),
                rule_sets=load_default_rule_sets(Path("review/rules")),
            )
            inputs = (
                RepositoryOnboardingInput(
                    RepositorySnapshot(101, "org/a", "main", "https://github.test/org/a"),
                    {"Python": 100},
                ),
            )
            (created,) = await sync.execute(provider_installation_id=target, repositories=inputs)
            clean_id = created.rule_version.repository_id
            assert clean_id != REPO_A
            await sync.disable(
                provider_installation_id=target,
                repositories=(RepositoryReference(101, "org/a", None, None),),
                delivery_id="round3-clean-target-removed",
            )
            async with factory() as session:
                assert (
                    await session.scalar(
                        text("SELECT installation_available FROM repositories WHERE id=:id"),
                        {"id": clean_id},
                    )
                    is False
                )
                assert (
                    await session.scalar(
                        text(
                            "SELECT count(*) FROM github_user_repository_access "
                            "WHERE provider_installation_id=:id"
                        ),
                        {"id": target},
                    )
                    == 0
                )
            for _ in range(2):
                (restored,) = await sync.execute(
                    provider_installation_id=target, repositories=inputs
                )
                assert restored.rule_version.repository_id == clean_id
            async with factory() as session:
                assert tuple(
                    (
                        await session.execute(
                            text(
                                "SELECT id, enabled, installation_available, max_comments "
                                "FROM repositories WHERE provider_installation_id=:id "
                                "AND external_id=101"
                            ),
                            {"id": target},
                        )
                    ).one()
                ) == (clean_id, True, True, 10)
                assert tuple(
                    (
                        await session.execute(
                            text(
                                "SELECT provider_installation_id, enabled, installation_available, "
                                "default_engine, wait_for_ci, max_comments, review_event, "
                                "prompt_version_id "
                                "FROM repositories WHERE id=:id"
                            ),
                            {"id": REPO_A},
                        )
                    ).one()
                ) == (INSTALL_A, False, False, "fast", "always", 7, "request_changes", PROMPT)
                assert (
                    await session.scalar(
                        text("SELECT count(*) FROM code_changes WHERE repository_id=:id"),
                        {"id": REPO_A},
                    )
                    == 3
                )
                assert await session.scalar(text("SELECT count(*) FROM runs")) == 4
                assert tuple(
                    (
                        await session.execute(
                            text(
                                "SELECT repository_id, agents_md FROM repo_conventions WHERE id=:id"
                            ),
                            {"id": _CONVENTIONS},
                        )
                    ).one()
                ) == (REPO_A, "custom rules")
                assert tuple(
                    (
                        await session.execute(
                            text("SELECT repository_id, is_active FROM rule_versions WHERE id=:id"),
                            {"id": RULE_A},
                        )
                    ).one()
                ) == (REPO_A, True)
                assert tuple(
                    (
                        await session.execute(
                            text(
                                "SELECT state, origin_provider_installation_id, rule_version_id "
                                "FROM runs WHERE id=:id"
                            ),
                            {"id": RUN_DONE},
                        )
                    ).one()
                ) == ("succeeded", INSTALL_A, RULE_A)
                assert tuple(
                    (
                        await session.execute(
                            text("SELECT run_id, tool FROM run_actions WHERE id=:id"),
                            {"id": _ACTION},
                        )
                    ).one()
                ) == (RUN_DONE, "fetch_diff")
                assert tuple(
                    (
                        await session.execute(
                            text("SELECT run_id, body FROM findings WHERE id=:id"),
                            {"id": UUID(int=77)},
                        )
                    ).one()
                ) == (RUN_DONE, "B")
                assert (
                    await session.scalar(
                        text("SELECT count(*) FROM code_changes WHERE repository_id=:id"),
                        {"id": clean_id},
                    )
                    == 0
                )
                assert await session.scalar(
                    text("SELECT daily_budget_usd FROM workspaces WHERE id=:id"),
                    {"id": WS_A},
                ) == Decimal("12.34")
        finally:
            await engine.dispose()

    asyncio.run(exercise())


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


@pytest.mark.integration
@pytest.mark.parametrize("first_user_repositories", [(), (103,)])
def test_positive_sync_waits_for_fresh_user_confirmation_before_preserving_archive(
    reinstall_env: Env,
    first_user_repositories: tuple[int, ...],
) -> None:
    """Created replay and added projections cannot preempt another user's archive."""

    async def exercise() -> None:
        engine = reinstall_env.engine()
        factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            await seed_archive(factory)
            assert await apply(
                factory,
                FreshGitHub(
                    installation=GitHubInstallation(29, "org", first_user_repositories, 700)
                ),
            ) == (WS_A,)
            async with factory() as session:
                target = await session.scalar(
                    text("SELECT id FROM provider_installations WHERE external_id=29")
                )
                assert isinstance(target, UUID)
            sync = SyncInstallationRepositories(
                uow_factory=lambda: SqlAlchemyInstallationRepositoriesUnitOfWork(factory),
                rule_sets=load_default_rule_sets(Path("review/rules")),
            )
            for _ in range(2):
                assert (
                    await sync.execute(
                        provider_installation_id=target,
                        repositories=(
                            RepositoryOnboardingInput(
                                RepositorySnapshot(
                                    101, "org/a", "main", "https://github.test/org/a"
                                ),
                                {"Python": 100},
                            ),
                        ),
                    )
                    == ()
                )
            async with factory() as session:
                assert (
                    await session.scalar(
                        text("SELECT count(*) FROM repositories WHERE external_id=101")
                    )
                    == 1
                )
                assert (
                    await session.scalar(
                        text("SELECT count(*) FROM rule_versions WHERE repository_id=:repo"),
                        {"repo": REPO_A},
                    )
                    == 1
                )
                assert (
                    await session.scalar(
                        text(
                            "SELECT count(*) FROM github_user_repository_access WHERE "
                            "repository_external_id=101"
                        )
                    )
                    == 0
                )
            assert await apply(factory, FreshGitHub(user_id=43)) == (WS_A,)
            async with factory() as session:
                row = (
                    await session.execute(
                        text(
                            "SELECT provider_installation_id, enabled, installation_available, "
                            "default_engine, "
                            "wait_for_ci, max_comments, review_event, prompt_version_id, "
                            "branch_filter, severity_threshold, rag_enabled FROM "
                            "repositories WHERE id=:repo"
                        ),
                        {"repo": REPO_A},
                    )
                ).one()
                assert tuple(row) == (
                    target,
                    False,
                    True,
                    "fast",
                    "always",
                    7,
                    "request_changes",
                    PROMPT,
                    ["main", "release/*"],
                    "critical",
                    True,
                )
                assert (
                    await session.scalar(
                        text("SELECT count(*) FROM code_changes WHERE repository_id=:repo"),
                        {"repo": REPO_A},
                    )
                    == 3
                )
                assert await session.scalar(text("SELECT count(*) FROM runs")) == 4
                assert tuple(
                    (
                        await session.scalars(
                            text(
                                "SELECT github_user_id FROM github_user_repository_access WHERE "
                                "repository_external_id=101"
                            )
                        )
                    ).all()
                ) == (43,)
                assert (
                    await session.scalar(
                        text("SELECT agents_md FROM repo_conventions WHERE id=:id"),
                        {"id": _CONVENTIONS},
                    )
                    == "custom rules"
                )
                assert (
                    await session.scalar(
                        text("SELECT tool FROM run_actions WHERE id=:id"), {"id": _ACTION}
                    )
                    == "fetch_diff"
                )
        finally:
            await engine.dispose()

    asyncio.run(exercise())
    with api(reinstall_env, AuthScope(42, (WS_A,))) as (client, _):
        assert client.get(f"/api/repos/{REPO_A}").status_code == 404
    with api(reinstall_env, AuthScope(43, (WS_A,))) as (client, _):
        assert client.get(f"/api/repos/{REPO_A}").status_code == 200
        assert client.get(f"/api/runs/{RUN_DONE}").status_code == 200


@pytest.mark.integration
@pytest.mark.parametrize("onboard_before_delete", [False, True])
@pytest.mark.parametrize("oauth_lists_repository", [False, True])
def test_late_source_deletion_permanently_keeps_clean_target_identity(
    reinstall_env: Env,
    onboard_before_delete: bool,
    oauth_lists_repository: bool,
) -> None:
    github = FreshGitHub(
        installation=GitHubInstallation(29, "org", (101,) if oauth_lists_repository else (), 700)
    )

    async def exercise() -> None:
        engine = reinstall_env.engine()
        factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            await seed_archive(factory)
            async with factory.begin() as session:
                await session.execute(
                    text("UPDATE provider_installations SET deleted_at=NULL WHERE id=:id"),
                    {"id": INSTALL_A},
                )
            (workspace,) = await apply(factory, github)
            assert workspace != WS_A
            async with factory() as session:
                target = await session.scalar(
                    text("SELECT id FROM provider_installations WHERE external_id=29")
                )
                assert isinstance(target, UUID)
            sync = SyncInstallationRepositories(
                uow_factory=lambda: SqlAlchemyInstallationRepositoriesUnitOfWork(factory),
                rule_sets=load_default_rule_sets(Path("review/rules")),
            )

            async def onboard() -> UUID:
                results = await sync.execute(
                    provider_installation_id=target,
                    repositories=(
                        RepositoryOnboardingInput(
                            RepositorySnapshot(101, "org/a", "main", "https://github.test/org/a"),
                            {"Python": 100},
                        ),
                    ),
                )
                assert len(results) == 1
                return results[0].rule_version.repository_id

            clean_id = await onboard() if onboard_before_delete else None
            async with factory.begin() as session:
                await SqlAlchemyInstallationRepositoryStore(session).disable_installation(INSTALL_A)
            assert await apply(factory, github) == (workspace,)
            if clean_id is None:
                clean_id = await onboard()
            assert await apply(factory, github) == (workspace,)
            assert await onboard() == clean_id
            assert clean_id != REPO_A
            async with factory() as session:
                assert tuple(
                    (
                        await session.execute(
                            text(
                                "SELECT "
                                "provider_installation_id,enabled,installation_available,"
                                "max_comments "
                                "FROM repositories WHERE id=:id"
                            ),
                            {"id": REPO_A},
                        )
                    ).one()
                ) == (INSTALL_A, False, False, 7)
                assert tuple(
                    (
                        await session.execute(
                            text(
                                "SELECT "
                                "provider_installation_id,enabled,installation_available,"
                                "max_comments "
                                "FROM repositories WHERE id=:id"
                            ),
                            {"id": clean_id},
                        )
                    ).one()
                ) == (target, True, True, 10)
                assert (
                    await session.scalar(
                        text("SELECT count(*) FROM code_changes WHERE repository_id=:repo"),
                        {"repo": REPO_A},
                    )
                    == 3
                )
                assert (
                    await session.scalar(
                        text("SELECT count(*) FROM code_changes WHERE repository_id=:repo"),
                        {"repo": clean_id},
                    )
                    == 0
                )
                assert await session.scalar(text("SELECT count(*) FROM runs")) == 4
                assert await session.scalar(
                    text(
                        "SELECT count(*) FROM github_user_repository_access "
                        "WHERE provider_installation_id=:id AND repository_external_id=101"
                    ),
                    {"id": target},
                ) == (1 if oauth_lists_repository else 0)
                assert (
                    await session.scalar(
                        text("SELECT count(*) FROM rule_versions WHERE repository_id=:repo"),
                        {"repo": clean_id},
                    )
                    == 1
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
        finally:
            await engine.dispose()

    asyncio.run(exercise())


@pytest.mark.integration
def test_callback_account_lock_waits_for_retirement_before_creating_missing_target(
    reinstall_env: Env,
) -> None:
    """No target rows exist: only the account advisory fence can block OAuth."""

    async def exercise() -> None:
        engine = reinstall_env.engine()
        factory = async_sessionmaker(engine, expire_on_commit=False)
        oauth: asyncio.Task[tuple[UUID, ...]] | None = None
        try:
            await seed_archive(factory)
            async with factory.begin() as session:
                await session.execute(
                    text("UPDATE provider_installations SET deleted_at=NULL WHERE id=:id"),
                    {"id": INSTALL_A},
                )
            async with factory.begin() as retiring:
                blocker_pid = await retiring.scalar(text("SELECT pg_backend_pid()"))
                key = int.from_bytes(
                    sha256(b"github-reinstall-account:700").digest()[:8], "big", signed=True
                )
                await retiring.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": key})
                oauth = asyncio.create_task(apply(factory, FreshGitHub()))
                async with asyncio.timeout(5):
                    async with factory() as observer:
                        while not await observer.scalar(
                            text(
                                "SELECT EXISTS (SELECT 1 FROM pg_stat_activity "
                                "WHERE wait_event='advisory' AND :pid = ANY(pg_blocking_pids(pid)))"
                            ),
                            {"pid": blocker_pid},
                        ):
                            assert not oauth.done(), (
                                "OAuth bypassed the account lock before retirement"
                            )
                            await observer.execute(text("SELECT pg_stat_clear_snapshot()"))
                            await asyncio.sleep(0.01)
                await SqlAlchemyInstallationRepositoryStore(retiring).disable_installation(
                    INSTALL_A
                )
            assert await asyncio.wait_for(oauth, 5) == (WS_A,)
            async with factory() as session:
                assert tuple(
                    (
                        await session.execute(
                            text(
                                "SELECT i.external_id, i.workspace_id, r.id, r.enabled, "
                                "r.max_comments "
                                "FROM repositories r JOIN provider_installations i ON "
                                "i.id=r.provider_installation_id "
                                "WHERE r.external_id=101"
                            )
                        )
                    ).one()
                ) == (29, WS_A, REPO_A, False, 7)
                assert await session.scalar(text("SELECT count(*) FROM runs")) == 4
        finally:
            if oauth is not None and not oauth.done():
                oauth.cancel()
                await asyncio.gather(oauth, return_exceptions=True)
            await engine.dispose()

    asyncio.run(exercise())


@pytest.mark.integration
def test_callback_and_positive_sync_wait_in_account_order_without_deadlock(
    reinstall_env: Env,
) -> None:
    async def exercise() -> None:
        engine = reinstall_env.engine()
        factory = async_sessionmaker(engine, expire_on_commit=False)
        tasks: list[asyncio.Task[object]] = []
        try:
            await seed_archive(factory)
            target = UUID(int=11311)
            async with factory.begin() as session:
                await session.execute(
                    text(
                        "INSERT INTO provider_installations "
                        "(id,workspace_id,provider,external_id,account_external_id,metadata) "
                        "VALUES (:id,:ws,'github',29,700,'{}')"
                    ),
                    {"id": target, "ws": WS_A},
                )
            sync = SyncInstallationRepositories(
                uow_factory=lambda: SqlAlchemyInstallationRepositoriesUnitOfWork(factory),
                rule_sets=load_default_rule_sets(Path("review/rules")),
            )
            async with factory.begin() as blocker:
                blocker_pid = await blocker.scalar(text("SELECT pg_backend_pid()"))
                key = int.from_bytes(
                    sha256(b"github-reinstall-account:700").digest()[:8], "big", signed=True
                )
                await blocker.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": key})
                callback = asyncio.create_task(apply(factory, FreshGitHub()))
                tasks.append(callback)
                async with factory() as observer:

                    async def wait_for_blocked(count: int) -> None:
                        async with asyncio.timeout(5):
                            while True:
                                await observer.execute(text("SELECT pg_stat_clear_snapshot()"))
                                waiting = await observer.scalar(
                                    text(
                                        "SELECT count(*) FROM pg_stat_activity WHERE "
                                        "wait_event='advisory' "
                                        "AND :pid=ANY(pg_blocking_pids(pid))"
                                    ),
                                    {"pid": blocker_pid},
                                )
                                if waiting == count:
                                    return
                                assert all(not task.done() for task in tasks)
                                await asyncio.sleep(0.01)

                    await wait_for_blocked(1)
                    projection = asyncio.create_task(
                        sync.execute(
                            provider_installation_id=target,
                            repositories=(
                                RepositoryOnboardingInput(
                                    RepositorySnapshot(
                                        101, "org/a", "main", "https://github.test/org/a"
                                    ),
                                    {"Python": 100},
                                ),
                            ),
                        )
                    )
                    tasks.append(projection)
                    await wait_for_blocked(2)
            assert await asyncio.wait_for(callback, 5) == (WS_A,)
            results = await asyncio.wait_for(projection, 5)
            assert len(results) == 1
            assert results[0].created is False
            assert results[0].rule_version.repository_id == REPO_A
            async with factory() as session:
                assert (
                    await session.scalar(
                        text("SELECT count(*) FROM repositories WHERE external_id=101")
                    )
                    == 1
                )
                assert tuple(
                    (
                        await session.execute(
                            text(
                                "SELECT provider_installation_id, enabled, max_comments "
                                "FROM repositories WHERE id=:id"
                            ),
                            {"id": REPO_A},
                        )
                    ).one()
                ) == (target, False, 7)
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await engine.dispose()

    asyncio.run(exercise())


@pytest.mark.integration
@pytest.mark.parametrize("preflight", [False, True])
def test_sync_restarts_after_null_account_becomes_numeric_without_callback_deadlock(
    reinstall_env: Env,
    preflight: bool,
) -> None:
    async def exercise() -> None:
        engine = reinstall_env.engine()
        from typing import Any

        from sqlalchemy import event

        @event.listens_for(engine.sync_engine, "connect")
        def set_lock_timeout(connection: Any, record: Any) -> None:
            cursor = connection.cursor()
            cursor.execute("SET lock_timeout = '2s'")
            cursor.close()

        factory = async_sessionmaker(engine, expire_on_commit=False)
        saw_null, resume_null = asyncio.Event(), asyncio.Event()
        saw_numeric, resume_numeric = asyncio.Event(), asyncio.Event()
        target = UUID(int=11312)
        sync_pid: int | None = None
        paused_null = False
        paused_numeric = False

        class TransitionSession(AsyncSession):
            async def get(self, entity: Any, ident: Any, **kwargs: Any) -> Any:
                nonlocal paused_null, paused_numeric, sync_pid
                row = await super().get(entity, ident, **kwargs)
                if ident == target and row is not None:
                    if row.account_external_id is None and not paused_null:
                        paused_null = True
                        saw_null.set()
                        await resume_null.wait()
                    elif row.account_external_id == 700 and not paused_numeric:
                        paused_numeric = True
                        sync_pid = await self.scalar(text("SELECT pg_backend_pid()"))
                        saw_numeric.set()
                        await resume_numeric.wait()
                return row

        sync_factory = async_sessionmaker[AsyncSession](
            engine, class_=TransitionSession, expire_on_commit=False
        )
        tasks: list[asyncio.Task[object]] = []
        try:
            await seed_archive(factory)
            async with factory.begin() as session:
                await session.execute(
                    text(
                        "INSERT INTO provider_installations "
                        "(id,workspace_id,provider,external_id,account_external_id,metadata) "
                        "VALUES (:id,:ws,'github',29,NULL,'{}')"
                    ),
                    {"id": target, "ws": WS_B},
                )
            sync = SyncInstallationRepositories(
                uow_factory=lambda: SqlAlchemyInstallationRepositoriesUnitOfWork(sync_factory),
                rule_sets=load_default_rule_sets(Path("review/rules")),
            )

            async def sync_operation() -> tuple[OnboardingResult, ...] | bool:
                if preflight:
                    return await sync.is_active(target)
                return await sync.execute(
                    provider_installation_id=target,
                    repositories=(
                        RepositoryOnboardingInput(
                            RepositorySnapshot(101, "org/a", "main", "https://github.test/org/a"),
                            {"Python": 100},
                        ),
                    ),
                )

            projection = asyncio.create_task(sync_operation())
            tasks.append(projection)
            await asyncio.wait_for(saw_null.wait(), 5)
            github = FreshGitHub(installation=GitHubInstallation(29, "org", (), 700))
            assert await asyncio.wait_for(apply(factory, github), 5) == (WS_B,)
            resume_null.set()
            await asyncio.wait_for(saw_numeric.wait(), 5)
            callback = asyncio.create_task(apply(factory, github))
            tasks.append(callback)
            async with asyncio.timeout(5):
                async with factory() as observer:
                    while not callback.done():
                        await observer.execute(text("SELECT pg_stat_clear_snapshot()"))
                        blocked = await observer.scalar(
                            text(
                                "SELECT EXISTS (SELECT 1 FROM pg_stat_activity "
                                "WHERE wait_event_type='Lock' AND :pid=ANY(pg_blocking_pids(pid)))"
                            ),
                            {"pid": sync_pid},
                        )
                        if blocked:
                            break
                        await asyncio.sleep(0.01)
            resume_numeric.set()
            assert await asyncio.wait_for(callback, 5) == (WS_B,)
            results = await asyncio.wait_for(projection, 5)
            async with factory() as session:
                if preflight:
                    assert results is True
                    assert (
                        await session.scalar(
                            text(
                                "SELECT count(*) FROM repositories "
                                "WHERE provider_installation_id=:id"
                            ),
                            {"id": target},
                        )
                        == 0
                    )
                else:
                    assert isinstance(results, tuple)
                    assert len(results) == 1
                    clean_id = results[0].rule_version.repository_id
                    assert clean_id != REPO_A
                    assert (
                        await session.scalar(
                            text("SELECT provider_installation_id FROM repositories WHERE id=:id"),
                            {"id": clean_id},
                        )
                        == target
                    )
                assert (
                    await session.scalar(
                        text("SELECT provider_installation_id FROM repositories WHERE id=:id"),
                        {"id": REPO_A},
                    )
                    == INSTALL_A
                )
        finally:
            resume_null.set()
            resume_numeric.set()
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await engine.dispose()

    asyncio.run(exercise())


async def wait_for_blocked_backend[T](
    observer: AsyncSession, blocker_pid: int, task: asyncio.Task[T]
) -> int:
    """Wait for a real PostgreSQL lock edge, failing if the operation escaped it."""
    async with asyncio.timeout(5):
        while True:
            await observer.execute(text("SELECT pg_stat_clear_snapshot()"))
            pid = await observer.scalar(
                text(
                    "SELECT pid FROM pg_stat_activity WHERE wait_event_type='Lock' "
                    "AND :blocker=ANY(pg_blocking_pids(pid)) ORDER BY pid LIMIT 1"
                ),
                {"blocker": blocker_pid},
            )
            if isinstance(pid, int):
                return pid
            assert not task.done(), "operation completed before the expected PostgreSQL lock wait"


async def finish_without_blocking_on[T](
    observer: AsyncSession, blocker_pid: int, task: asyncio.Task[T]
) -> T:
    """A disjoint target must finish while the competing transaction is still fenced."""
    async with asyncio.timeout(5):
        while not task.done():
            await observer.execute(text("SELECT pg_stat_clear_snapshot()"))
            blocked = await observer.scalar(
                text(
                    "SELECT EXISTS (SELECT 1 FROM pg_stat_activity WHERE wait_event_type='Lock' "
                    "AND :blocker=ANY(pg_blocking_pids(pid)))"
                ),
                {"blocker": blocker_pid},
            )
            assert not blocked, "operation acquired an earlier target before its ordered prelock"
        return await task


@pytest.mark.integration
@pytest.mark.parametrize("existing_lower_target", [False, True])
def test_overlapping_null_account_callbacks_prelock_before_creating_targets(
    reinstall_env: Env,
    existing_lower_target: bool,
) -> None:
    """NULL snapshots need row ordering even without an account advisory fence."""

    @dataclass
    class CallbackGitHub:
        user_id: int
        installations: tuple[GitHubInstallation, ...]

        async def identify_user(self, access_token: str) -> int:
            return self.user_id

        async def list_for_user(self, access_token: str) -> AuthenticatedGitHubInstallations:
            return AuthenticatedGitHubInstallations(self.user_id, self.installations)

    async def exercise() -> None:
        engine = reinstall_env.engine()
        factory = async_sessionmaker(engine, expire_on_commit=False)
        tasks: list[asyncio.Task[tuple[UUID, ...]]] = []

        async def callback(github: CallbackGitHub) -> tuple[UUID, ...]:
            return await LinkGitHubInstallations(
                github=github,
                uow_factory=lambda: SqlAlchemyGitHubInstallationLinkUnitOfWork(factory),
            ).execute("fresh-user-token")

        try:
            await seed_archive(factory)
            async with factory.begin() as session:
                for external_id in (23, 29) if existing_lower_target else (29,):
                    await session.execute(
                        text(
                            "INSERT INTO provider_installations "
                            "(id,workspace_id,provider,external_id,account_external_id,metadata) "
                            "VALUES (:id,:ws,'github',:external,NULL,'{}')"
                        ),
                        {"id": UUID(int=11320 + external_id), "ws": WS_B, "external": external_id},
                    )
            async with factory.begin() as blocker:
                blocker_pid = await blocker.scalar(text("SELECT pg_backend_pid()"))
                assert isinstance(blocker_pid, int)
                await blocker.execute(
                    text(
                        "SELECT id FROM provider_installations WHERE external_id=29 "
                        "FOR NO KEY UPDATE"
                    )
                )
                # Different, deliberately reversed input orders share installation 23.
                first = asyncio.create_task(
                    callback(
                        CallbackGitHub(
                            42,
                            (
                                GitHubInstallation(29, "org", (), None),
                                GitHubInstallation(23, "org", (), None),
                            ),
                        )
                    )
                )
                tasks.append(first)
                async with factory() as observer:
                    first_pid = await wait_for_blocked_backend(observer, blocker_pid, first)
                    second = asyncio.create_task(
                        callback(
                            CallbackGitHub(
                                43,
                                (
                                    GitHubInstallation(31, "org", (), None),
                                    GitHubInstallation(23, "org", (), None),
                                ),
                            )
                        )
                    )
                    tasks.append(second)
                    if existing_lower_target:
                        # All existing targets are locked in ascending order: 23 is
                        # owned before the first callback waits on higher target 29.
                        await wait_for_blocked_backend(observer, first_pid, second)
                    else:
                        # Missing 23 must not be inserted before the prelock on 29.
                        # The independent callback can establish it and commit now.
                        assert (
                            len(await finish_without_blocking_on(observer, first_pid, second)) == 2
                        )
            assert len(await asyncio.wait_for(first, 5)) == 2
            assert len(await asyncio.wait_for(second, 5)) == 2
            async with factory() as session:
                assert tuple(
                    (
                        await session.execute(
                            text(
                                "SELECT external_id, count(*), "
                                "bool_and(account_external_id IS NULL) "
                                "FROM provider_installations WHERE external_id IN (23,29,31) "
                                "GROUP BY external_id ORDER BY external_id"
                            )
                        )
                    )
                    .tuples()
                    .all()
                ) == ((23, 1, True), (29, 1, True), (31, 1, True))
                assert tuple(
                    (
                        await session.execute(
                            text(
                                "SELECT github_user_id, count(*) FROM github_user_workspace_access "
                                "WHERE github_user_id IN (42,43) "
                                "GROUP BY github_user_id ORDER BY github_user_id"
                            )
                        )
                    )
                    .tuples()
                    .all()
                ) == ((42, 1 if existing_lower_target else 2), (43, 2))
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 5)
            await engine.dispose()

    asyncio.run(exercise())


@pytest.mark.integration
def test_sync_prelocks_archive_before_target_without_blocking_null_account_callback(
    reinstall_env: Env,
) -> None:
    """A NULL snapshot callback bypasses account serialization and exposes row inversions."""

    async def exercise() -> None:
        engine = reinstall_env.engine()
        factory = async_sessionmaker(engine, expire_on_commit=False)
        projection: asyncio.Task[tuple[OnboardingResult, ...]] | None = None
        callback: asyncio.Task[tuple[UUID, ...]] | None = None
        target = UUID(int=11360)
        try:
            await seed_archive(factory)
            async with factory.begin() as session:
                await session.execute(
                    text(
                        "INSERT INTO provider_installations "
                        "(id,workspace_id,provider,external_id,account_external_id,metadata) "
                        "VALUES (:id,:ws,'github',29,700,'{}')"
                    ),
                    {"id": target, "ws": WS_A},
                )
            sync = SyncInstallationRepositories(
                uow_factory=lambda: SqlAlchemyInstallationRepositoriesUnitOfWork(factory),
                rule_sets=load_default_rule_sets(Path("review/rules")),
            )
            async with factory.begin() as blocker:
                blocker_pid = await blocker.scalar(text("SELECT pg_backend_pid()"))
                assert isinstance(blocker_pid, int)
                await blocker.execute(
                    text("SELECT id FROM provider_installations WHERE id=:id FOR NO KEY UPDATE"),
                    {"id": INSTALL_A},
                )
                projection = asyncio.create_task(
                    sync.execute(
                        provider_installation_id=target,
                        repositories=(
                            RepositoryOnboardingInput(
                                RepositorySnapshot(
                                    101, "org/a", "main", "https://github.test/org/a"
                                ),
                                {"Python": 100},
                            ),
                        ),
                    )
                )
                async with factory() as observer:
                    sync_pid = await wait_for_blocked_backend(observer, blocker_pid, projection)
                    callback = asyncio.create_task(
                        apply(
                            factory,
                            FreshGitHub(
                                user_id=43, installation=GitHubInstallation(29, "org", (), None)
                            ),
                        )
                    )
                    assert await finish_without_blocking_on(observer, sync_pid, callback) == (WS_A,)
            assert await asyncio.wait_for(projection, 5) == ()
            async with factory() as session:
                assert (
                    await session.scalar(
                        text(
                            "SELECT count(*) FROM repositories WHERE provider_installation_id=:id"
                        ),
                        {"id": target},
                    )
                    == 0
                )
                assert tuple(
                    (
                        await session.execute(
                            text(
                                "SELECT provider_installation_id, enabled, installation_available "
                                "FROM repositories WHERE id=:id"
                            ),
                            {"id": REPO_A},
                        )
                    ).one()
                ) == (INSTALL_A, False, False)
                assert (
                    await session.scalar(
                        text("SELECT account_external_id FROM provider_installations WHERE id=:id"),
                        {"id": target},
                    )
                    == 700
                )
        finally:
            tasks = [task for task in (projection, callback) if task is not None]
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 5)
            await engine.dispose()

    asyncio.run(exercise())
