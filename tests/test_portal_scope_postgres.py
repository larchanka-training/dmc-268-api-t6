"""Opt-in PostgreSQL proof for claim, current grant, and repository intersection."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from sqlalchemy.schema import CreateSchema, DropSchema

from alembic import command
from app.bootstrap.portal_auth import get_auth_scope
from app.bootstrap.reviews_api import ReviewsApiResources
from app.main import app
from app.modules.auth.application.scope import AuthScope

WORKSPACE_A = UUID("00000000-0000-0000-0000-000000000011")
WORKSPACE_B = UUID("00000000-0000-0000-0000-000000000012")
INSTALLATION_A = UUID("00000000-0000-0000-0000-000000000021")
INSTALLATION_B = UUID("00000000-0000-0000-0000-000000000022")
REPOS = tuple(UUID(f"00000000-0000-0000-0000-{value:012d}") for value in (31, 32, 33))
PULLS = tuple(UUID(f"00000000-0000-0000-0000-{value:012d}") for value in (41, 42, 43))
RUNS = tuple(UUID(f"00000000-0000-0000-0000-{value:012d}") for value in (51, 52, 53))
RULES = tuple(UUID(f"00000000-0000-0000-0000-{value:012d}") for value in (61, 62, 63))
PROMPT = UUID("00000000-0000-0000-0000-000000000071")


@pytest.fixture
def portal_database() -> Iterator[tuple[str, str]]:
    database_url = os.environ.get("TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("set TEST_DATABASE_URL to run PostgreSQL integration tests")
    schema = f"test_portal_scope_{uuid4().hex}"
    engine = create_engine(database_url)
    try:
        with engine.connect() as connection:
            connection.execute(CreateSchema(schema))
            connection.execute(text(f'SET search_path TO "{schema}"'))
            connection.commit()
            config = Config("alembic.ini")
            config.attributes["connection"] = connection
            command.upgrade(config, "head")
            for workspace_id, name in ((WORKSPACE_A, "Alpha"), (WORKSPACE_B, "Beta")):
                connection.execute(
                    text(
                        "INSERT INTO workspaces (id, name, daily_budget_usd) VALUES (:id, :name, 0)"
                    ),
                    {"id": workspace_id, "name": name},
                )
            for installation_id, workspace_id, external_id in (
                (INSTALLATION_A, WORKSPACE_A, 17),
                (INSTALLATION_B, WORKSPACE_B, 18),
            ):
                connection.execute(
                    text(
                        "INSERT INTO provider_installations "
                        "(id, workspace_id, provider, external_id, metadata) "
                        "VALUES (:id, :workspace, 'github', :external, '{}'::jsonb)"
                    ),
                    {"id": installation_id, "workspace": workspace_id, "external": external_id},
                )
            for user_id, login in ((42, "alice"), (43, "bob")):
                connection.execute(
                    text(
                        "INSERT INTO github_user_profiles (id, login, name) "
                        "VALUES (:id, :login, :login)"
                    ),
                    {"id": user_id, "login": login},
                )
            for user_id, workspace_id in ((42, WORKSPACE_A), (42, WORKSPACE_B), (43, WORKSPACE_A)):
                connection.execute(
                    text(
                        "INSERT INTO github_user_workspace_access (github_user_id, workspace_id) "
                        "VALUES (:user, :workspace)"
                    ),
                    {"user": user_id, "workspace": workspace_id},
                )
            for user_id, installation_id, repository_external_id in (
                (42, INSTALLATION_A, 101),
                (42, INSTALLATION_B, 103),
                (43, INSTALLATION_A, 102),
            ):
                connection.execute(
                    text(
                        "INSERT INTO github_user_repository_access "
                        "(github_user_id, provider_installation_id, repository_external_id) "
                        "VALUES (:user, :installation, :repository)"
                    ),
                    {
                        "user": user_id,
                        "installation": installation_id,
                        "repository": repository_external_id,
                    },
                )
            connection.execute(
                text(
                    "INSERT INTO prompt_versions (id, key, version, content, checksum, is_active) "
                    "VALUES (:id, 'review.system', 1, 'system', :checksum, true)"
                ),
                {"id": PROMPT, "checksum": "c" * 64},
            )
            now = datetime.now(UTC)
            records = zip(
                REPOS,
                PULLS,
                RUNS,
                RULES,
                (INSTALLATION_A, INSTALLATION_A, INSTALLATION_B),
                (101, 102, 103),
                strict=True,
            )
            for index, record in enumerate(records, start=1):
                repo_id, pull_id, run_id, rule_id, installation_id, repo_external = record
                connection.execute(
                    text(
                        "INSERT INTO repositories "
                        "(id, provider_installation_id, external_id, full_name, "
                        "default_branch, web_url) "
                        "VALUES (:id, :installation, :external, :name, 'main', :url)"
                    ),
                    {
                        "id": repo_id,
                        "installation": installation_id,
                        "external": repo_external,
                        "name": f"octo/repo-{index}",
                        "url": f"https://github.test/octo/repo-{index}",
                    },
                )
                connection.execute(
                    text(
                        "INSERT INTO rule_versions "
                        "(id, repository_id, version, rules, checksum, is_active) "
                        "VALUES (:id, :repository, 1, '[]'::jsonb, :checksum, true)"
                    ),
                    {"id": rule_id, "repository": repo_id, "checksum": str(index) * 64},
                )
                connection.execute(
                    text(
                        "INSERT INTO code_changes "
                        "(id, repository_id, external_id, external_number, title, source_branch, "
                        "target_branch, base_sha, head_sha, state, web_url) "
                        "VALUES (:id, :repository, :external, :number, 'PR', 'feature', 'main', "
                        ":base, :head, 'open', :url)"
                    ),
                    {
                        "id": pull_id,
                        "repository": repo_id,
                        "external": 900 + index,
                        "number": index,
                        "base": "b" * 40,
                        "head": "e" * 40,
                        "url": f"https://github.test/octo/repo-{index}/pull/{index}",
                    },
                )
                connection.execute(
                    text(
                        "INSERT INTO runs "
                        "(id, code_change_id, base_sha, head_sha, state, trigger, idempotency_key, "
                        "engine, rule_version_id, prompt_version_id, available_at, created_at) "
                        "VALUES (:id, :pr, :base, :head, 'queued', 'manual', :key, "
                        "'fast', :rule, :prompt, :available, :created)"
                    ),
                    {
                        "id": run_id,
                        "pr": pull_id,
                        "base": "b" * 40,
                        "head": "e" * 40,
                        "key": str(index) * 64,
                        "rule": rule_id,
                        "prompt": PROMPT,
                        "available": now,
                        "created": now + timedelta(seconds={1: 1, 2: 2, 3: 0}[index]),
                    },
                )
                connection.execute(
                    text(
                        "INSERT INTO run_actions "
                        "(id, run_id, index, tool, request, response, started_at, duration_ms) "
                        "VALUES (:id, :run, 0, 'test', '{}'::jsonb, CAST(:response AS jsonb), "
                        ":started, 1)"
                    ),
                    {
                        "id": uuid4(),
                        "run": run_id,
                        "response": '{"secret":true}',
                        "started": now,
                    },
                )
                connection.execute(
                    text(
                        "INSERT INTO code_change_diffs "
                        "(id, run_id, code_change_id, head_sha, filename, patch, blob_sha) "
                        "VALUES (:id, :run, :pr, :head, 'src/a.py', '@@ -1 +1 @@', :blob)"
                    ),
                    {
                        "id": uuid4(),
                        "run": run_id,
                        "pr": pull_id,
                        "head": "e" * 40,
                        "blob": str(index) * 40,
                    },
                )
                connection.execute(
                    text(
                        "INSERT INTO cached_file_blobs "
                        "(id, repository_id, blob_sha, content, expires_at) "
                        "VALUES (:id, :repository, :blob, 'line', :expires)"
                    ),
                    {
                        "id": uuid4(),
                        "repository": repo_id,
                        "blob": str(index) * 40,
                        "expires": now + timedelta(days=7),
                    },
                )
            connection.commit()
            yield database_url, schema
            connection.rollback()
            connection.execute(text("SET search_path TO public"))
            connection.execute(DropSchema(schema, cascade=True))
            connection.commit()
    finally:
        engine.dispose()


@pytest.mark.integration
def test_portal_routes_intersect_claim_current_membership_and_repository_grant(
    portal_database: tuple[str, str],
) -> None:
    database_url, schema = portal_database
    engine = create_async_engine(
        database_url,
        connect_args={"options": f"-csearch_path={schema}"},
        poolclass=NullPool,
    )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    app.state.reviews_api_resources = ReviewsApiResources(engine, factory)
    app.dependency_overrides[get_auth_scope] = lambda: AuthScope(42, (WORKSPACE_A,))
    try:
        client = TestClient(app)
        limited = client.get("/api/runs", params={"limit": 1})
        assert limited.status_code == 200
        assert [item["id"] for item in limited.json()["items"]] == [str(RUNS[0])]
        app.dependency_overrides[get_auth_scope] = lambda: AuthScope(42, (WORKSPACE_A, WORKSPACE_B))
        first_page = client.get("/api/runs", params={"limit": 1}).json()
        second_page = client.get(
            "/api/runs", params={"limit": 1, "cursor": first_page["nextCursor"]}
        ).json()
        assert [item["id"] for item in first_page["items"]] == [str(RUNS[0])]
        assert [item["id"] for item in second_page["items"]] == [str(RUNS[2])]
        app.dependency_overrides[get_auth_scope] = lambda: AuthScope(42, (WORKSPACE_A,))
        assert client.get(f"/api/runs/{RUNS[0]}").status_code == 200
        assert client.get(f"/api/runs/{RUNS[2]}").status_code == 404
        denied = RUNS[1]
        for path in (
            f"/api/runs/{denied}",
            f"/api/runs/{denied}/comments",
            f"/api/runs/{denied}/actions",
            f"/api/runs/{denied}/actions/0/response",
            f"/api/runs/{denied}/diff",
            f"/api/runs/{denied}/files?path=src/a.py",
        ):
            assert client.get(path).status_code == 404
        assert client.post(f"/api/runs/{denied}/cancel").status_code == 404

        async def denied_state() -> str:
            async with factory() as session:
                return str(
                    await session.scalar(
                        text("SELECT state FROM runs WHERE id = :id"), {"id": denied}
                    )
                )

        assert asyncio.run(denied_state()) == "queued"
        assert client.get("/api/auth/me").json()["workspaces"] == [
            {"id": str(WORKSPACE_A), "name": "Alpha", "installationId": 17}
        ]

        async def revoke_repo_grant() -> None:
            async with factory.begin() as session:
                await session.execute(
                    text(
                        "DELETE FROM github_user_repository_access "
                        "WHERE github_user_id = 42 AND provider_installation_id = :installation "
                        "AND repository_external_id = 101"
                    ),
                    {"installation": INSTALLATION_A},
                )

        asyncio.run(revoke_repo_grant())
        assert client.get("/api/runs").json()["items"] == []
        assert client.get(f"/api/runs/{RUNS[0]}").status_code == 404
        app.dependency_overrides[get_auth_scope] = lambda: AuthScope(43, (WORKSPACE_A,))
        assert [item["id"] for item in client.get("/api/runs").json()["items"]] == [str(RUNS[1])]
        assert client.get(f"/api/runs/{RUNS[0]}").status_code == 404
        app.dependency_overrides[get_auth_scope] = lambda: AuthScope(42, (WORKSPACE_B,))
        assert [item["id"] for item in client.get("/api/runs").json()["items"]] == [str(RUNS[2])]

        async def revoke_workspace_grant() -> None:
            async with factory.begin() as session:
                await session.execute(
                    text(
                        "DELETE FROM github_user_workspace_access "
                        "WHERE github_user_id = 42 AND workspace_id = :workspace"
                    ),
                    {"workspace": WORKSPACE_B},
                )

        asyncio.run(revoke_workspace_grant())
        assert client.get("/api/runs").json()["items"] == []
        assert client.get("/api/auth/me").json()["workspaces"] == []
        app.dependency_overrides[get_auth_scope] = lambda: AuthScope(42, ())
        assert client.get("/api/runs").json() == {"items": [], "nextCursor": None}
        assert client.get(f"/api/runs/{RUNS[0]}").status_code == 404
        assert client.get("/api/repos").status_code == 200
        assert client.get("/api/repos").json() == []
        assert client.get("/api/auth/me").json()["workspaces"] == []
    finally:
        app.dependency_overrides.clear()
        del app.state.reviews_api_resources
        asyncio.run(engine.dispose())


@pytest.mark.integration
def test_installation_removal_revokes_all_users_but_manual_disable_preserves_access(
    portal_database: tuple[str, str],
) -> None:
    from app.modules.repositories.application.installation_repositories import RepositoryReference
    from app.modules.repositories.application.sync_installation_repositories import (
        SyncInstallationRepositories,
    )
    from app.modules.repositories.infrastructure.installation_repository_unit_of_work import (
        SqlAlchemyInstallationRepositoriesUnitOfWork,
    )

    database_url, schema = portal_database
    engine = create_async_engine(
        database_url, connect_args={"options": f"-csearch_path={schema}"}, poolclass=NullPool
    )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    sync = SyncInstallationRepositories(
        uow_factory=lambda: SqlAlchemyInstallationRepositoriesUnitOfWork(factory), rule_sets={}
    )

    async def prepare() -> None:
        async with factory.begin() as session:
            await session.execute(
                text(
                    "INSERT INTO github_user_repository_access "
                    "(github_user_id, provider_installation_id, repository_external_id) "
                    "VALUES (43, :installation, 101)"
                ),
                {"installation": INSTALLATION_A},
            )

    asyncio.run(prepare())
    app.state.reviews_api_resources = ReviewsApiResources(engine, factory)
    app.dependency_overrides[get_auth_scope] = lambda: AuthScope(42, (WORKSPACE_A, WORKSPACE_B))
    try:
        client = TestClient(app)
        assert client.patch(f"/api/repos/{REPOS[0]}", json={"enabled": False}).status_code == 200
        assert {item["id"] for item in client.get("/api/repos").json()} == {
            str(REPOS[0]),
            str(REPOS[2]),
        }
        for _ in range(2):
            asyncio.run(
                sync.disable(
                    delivery_id="removal-delivery",
                    provider_installation_id=INSTALLATION_A,
                    repositories=(RepositoryReference(101, "octo/repo-1", None, None),),
                )
            )
        assert [item["id"] for item in client.get("/api/repos").json()] == [str(REPOS[2])]
        assert client.get(f"/api/runs/{RUNS[0]}").status_code == 404
        app.dependency_overrides[get_auth_scope] = lambda: AuthScope(43, (WORKSPACE_A,))
        assert [item["id"] for item in client.get("/api/repos").json()] == [str(REPOS[1])]
        assert client.get(f"/api/runs/{RUNS[0]}").status_code == 404
    finally:
        app.dependency_overrides.clear()
        del app.state.reviews_api_resources
        asyncio.run(engine.dispose())


@pytest.mark.integration
@pytest.mark.parametrize("payload_repositories", [None, (), (101,)])
def test_installation_deleted_revokes_unlisted_grants_without_github_calls(
    portal_database: tuple[str, str], payload_repositories: tuple[int, ...] | None
) -> None:
    from dataclasses import replace
    from typing import Any, cast

    from app.modules.integrations.webhooks.api.installation_event_dtos import (
        parse_installation_repositories_event,
    )
    from app.modules.integrations.webhooks.application.installation_event_projector import (
        InstallationEventProjector,
    )
    from app.modules.repositories.application.sync_installation_repositories import (
        SyncInstallationRepositories,
    )
    from app.modules.repositories.infrastructure.installation_repository_unit_of_work import (
        SqlAlchemyInstallationRepositoriesUnitOfWork,
    )

    database_url, schema = portal_database
    engine = create_async_engine(
        database_url, connect_args={"options": f"-csearch_path={schema}"}, poolclass=NullPool
    )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    # Any attempt to call GitHub fails: deleted events need no provider methods.
    no_github = cast(Any, object())
    projector = InstallationEventProjector(
        tree_provider=no_github,
        label_provider=no_github,
        details_provider=no_github,
        sync=SyncInstallationRepositories(
            uow_factory=lambda: SqlAlchemyInstallationRepositoriesUnitOfWork(factory), rule_sets={}
        ),
    )

    async def exercise() -> None:
        async with factory.begin() as session:
            # A grant can exist before repository onboarding; deletion must clear it too.
            await session.execute(
                text(
                    "INSERT INTO github_user_repository_access "
                    "(github_user_id, provider_installation_id, repository_external_id) "
                    "VALUES (43, :installation, 999)"
                ),
                {"installation": INSTALLATION_A},
            )
        payload: dict[str, object] = {"action": "deleted", "installation": {"id": 17}}
        if payload_repositories is not None:
            payload["repositories"] = [
                {"id": repo_id, "full_name": "octo/repo"} for repo_id in payload_repositories
            ]
        event = replace(
            parse_installation_repositories_event(event_name="installation", payload=payload),
            delivery_id="deletion-delivery",
        )
        for _ in range(2):
            await projector.execute(
                provider_installation_id=INSTALLATION_A,
                event=event,
            )
        async with factory() as session:
            rows = (
                await session.execute(
                    text(
                        "SELECT github_user_id, repository_external_id "
                        "FROM github_user_repository_access ORDER BY github_user_id"
                    )
                )
            ).all()
            assert [tuple(row) for row in rows] == [(42, 103)]
            enabled = (
                await session.execute(
                    text("SELECT external_id, enabled FROM repositories ORDER BY external_id")
                )
            ).all()
            assert [tuple(row) for row in enabled] == [(101, False), (102, False), (103, True)]
        await engine.dispose()

    asyncio.run(exercise())


@pytest.mark.integration
@pytest.mark.parametrize("delete_installation", [False, True])
@pytest.mark.parametrize("user_id", [42, 43])
def test_removal_during_oauth_snapshot_cannot_restore_grants(
    portal_database: tuple[str, str], delete_installation: bool, user_id: int
) -> None:
    import json
    from typing import Any, cast

    from app.modules.integrations.webhooks.api.dispatch import GitHubWebhookDispatchAdapter
    from app.modules.integrations.webhooks.application.github_installation_dispatch import (
        GitHubDispatchEvent,
        GitHubInstallationDeliveryDispatcher,
    )
    from app.modules.integrations.webhooks.application.installation_event_projector import (
        InstallationEventProjector,
    )
    from app.modules.integrations.webhooks.application.receive_github_delivery import (
        ReceiveGitHubDelivery,
        WebhookReceipt,
    )
    from app.modules.integrations.webhooks.infrastructure.github_webhook_receipts import (
        SqlAlchemyGitHubWebhookReceiptStore,
        SqlAlchemyGitHubWebhookReceiptUnitOfWork,
    )
    from app.modules.repositories.application.installation_repositories import (
        InstallationRepositoriesEvent,
        RepositoryReference,
    )
    from app.modules.repositories.application.sync_installation_repositories import (
        SyncInstallationRepositories,
    )
    from app.modules.repositories.infrastructure.installation_repository_unit_of_work import (
        SqlAlchemyInstallationRepositoriesUnitOfWork,
    )
    from app.modules.workspaces.application.link_github_installations import (
        AuthenticatedGitHubInstallations,
        GitHubInstallation,
        LinkGitHubInstallations,
    )
    from app.modules.workspaces.infrastructure.github_installation_links import (
        SqlAlchemyGitHubInstallationLinkUnitOfWork,
    )

    database_url, schema = portal_database
    engine = create_async_engine(
        database_url, connect_args={"options": f"-csearch_path={schema}"}, poolclass=NullPool
    )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    sync = SyncInstallationRepositories(
        uow_factory=lambda: SqlAlchemyInstallationRepositoriesUnitOfWork(factory), rule_sets={}
    )

    class Resolver:
        async def find_github_installation_id(self, external_id: int) -> UUID | None:
            return INSTALLATION_A

    no_github = cast(Any, object())
    dispatcher = GitHubInstallationDeliveryDispatcher(
        resolver=Resolver(),
        onboarding=InstallationEventProjector(
            tree_provider=no_github,
            label_provider=no_github,
            details_provider=no_github,
            sync=sync,
        ),
    )
    removal = InstallationRepositoriesEvent(
        installation_external_id=17,
        action="deleted" if delete_installation else "removed",
        added_repositories=(),
        removed_repositories=(RepositoryReference(101, "octo/repo-1", None, None),),
    )

    async def exercise() -> None:
        now = datetime.now(UTC)
        fail_finalize = True

        class FailingReceiptStore(SqlAlchemyGitHubWebhookReceiptStore):
            async def mark_projected(self, delivery_id: str, token: UUID, at: datetime) -> None:
                if fail_finalize:
                    raise RuntimeError("receipt finalization unavailable")
                await super().mark_projected(delivery_id, token, at)

        class ReceiptUow(SqlAlchemyGitHubWebhookReceiptUnitOfWork):
            @property
            def receipts(self) -> FailingReceiptStore:
                return FailingReceiptStore(self.session)

        receiver = ReceiveGitHubDelivery(
            uow_factory=lambda: ReceiptUow(factory),
            dispatcher=GitHubWebhookDispatchAdapter(dispatcher),
            now=lambda: now,
        )
        receipt = WebhookReceipt(
            "original-removal",
            "installation" if delete_installation else "installation_repositories",
            json.dumps(
                {
                    "installation": {"id": 17},
                    "action": removal.action,
                    "repositories": [],
                    "repositories_removed": [{"id": 101, "full_name": "octo/repo-1"}],
                }
            ),
        )
        await receiver.execute(receipt)
        snapshot_started = asyncio.Event()
        snapshot_release = asyncio.Event()

        class GitHub:
            async def identify_user(self, access_token: str) -> int:
                return user_id

            async def list_for_user(self, access_token: str) -> AuthenticatedGitHubInstallations:
                snapshot_started.set()
                await snapshot_release.wait()
                return AuthenticatedGitHubInstallations(
                    user_id,
                    (
                        GitHubInstallation(17, "alpha", (101,)),
                        GitHubInstallation(18, "beta", (103,)),
                    ),
                )

        link = LinkGitHubInstallations(
            github=GitHub(), uow_factory=lambda: SqlAlchemyGitHubInstallationLinkUnitOfWork(factory)
        )
        pending = asyncio.create_task(link.execute("stale-snapshot"))
        await asyncio.wait_for(snapshot_started.wait(), timeout=5)
        assert await receiver.replay_pending() == 0  # Effect commits, finalization fails.
        snapshot_release.set()
        await asyncio.wait_for(pending, timeout=5)
        async with factory() as session:
            grants = (
                (
                    await session.execute(
                        text(
                            "SELECT repository_external_id FROM github_user_repository_access "
                            "WHERE github_user_id=:user_id ORDER BY repository_external_id"
                        ),
                        {"user_id": user_id},
                    )
                )
                .scalars()
                .all()
            )
            assert grants == [103]
        # A fresh GitHub snapshot after re-adding access can grant it again.
        await link.execute("fresh-snapshot")
        async with factory() as session:
            grants = (
                (
                    await session.execute(
                        text(
                            "SELECT repository_external_id FROM github_user_repository_access "
                            "WHERE github_user_id=:user_id ORDER BY repository_external_id"
                        ),
                        {"user_id": user_id},
                    )
                )
                .scalars()
                .all()
            )
            assert grants == [101, 103]
        # Finalization may fail after the effect commits. Re-add the repository and
        # replay that old delivery after fresh OAuth: it must preserve the grant.
        async with factory.begin() as session:
            await session.execute(text("UPDATE repositories SET enabled = true"))
            revoked_at = await session.scalar(
                text(
                    "SELECT revoked_at FROM github_installation_access_revocations "
                    "WHERE provider_installation_id = :installation"
                ),
                {"installation": INSTALLATION_A},
            )
        fail_finalize = False
        now += timedelta(minutes=10)  # Expire the failed attempt's durable claim.
        assert await receiver.replay_pending() == 1
        async with factory() as session:
            assert (
                await session.scalar(
                    text(
                        "SELECT count(*) FROM github_user_repository_access "
                        "WHERE github_user_id=:user_id AND repository_external_id=101"
                    ),
                    {"user_id": user_id},
                )
                == 1
            )
            assert (
                await session.scalar(text("SELECT enabled FROM repositories WHERE external_id=101"))
                is True
            )
            assert (
                await session.scalar(
                    text(
                        "SELECT revoked_at FROM github_installation_access_revocations "
                        "WHERE provider_installation_id=:installation"
                    ),
                    {"installation": INSTALLATION_A},
                )
                == revoked_at
            )
        # A genuinely new delivery must still revoke the restored access.
        await dispatcher.execute(GitHubDispatchEvent("later-removal", removal))
        async with factory() as session:
            assert (
                await session.scalar(
                    text(
                        "SELECT count(*) FROM github_user_repository_access "
                        "WHERE github_user_id=:user_id AND repository_external_id=101"
                    ),
                    {"user_id": user_id},
                )
                == 0
            )
        await engine.dispose()

    asyncio.run(exercise())


@pytest.mark.integration
def test_removal_rollback_preserves_grant_and_allows_same_delivery_retry(
    portal_database: tuple[str, str],
) -> None:
    from app.modules.repositories.application.installation_repositories import RepositoryReference
    from app.modules.repositories.application.sync_installation_repositories import (
        SyncInstallationRepositories,
    )
    from app.modules.repositories.infrastructure.installation_repository_unit_of_work import (
        SqlAlchemyInstallationRepositoriesUnitOfWork,
    )

    database_url, schema = portal_database
    engine = create_async_engine(
        database_url, connect_args={"options": f"-csearch_path={schema}"}, poolclass=NullPool
    )
    factory = async_sessionmaker(engine, expire_on_commit=False)

    class FailingCommit(SqlAlchemyInstallationRepositoriesUnitOfWork):
        async def commit(self) -> None:
            raise RuntimeError("commit unavailable")

    async def exercise() -> None:
        arguments = (RepositoryReference(101, "octo/repo-1", None, None),)
        failing = SyncInstallationRepositories(
            uow_factory=lambda: FailingCommit(factory), rule_sets={}
        )
        with pytest.raises(RuntimeError, match="commit unavailable"):
            await failing.disable(
                provider_installation_id=INSTALLATION_A,
                repositories=arguments,
                delivery_id="retry-after-rollback",
            )
        async with factory() as session:
            assert (
                await session.scalar(
                    text("SELECT count(*) FROM github_installation_removal_effects")
                )
                == 0
            )
            assert (
                await session.scalar(
                    text("SELECT count(*) FROM github_installation_access_revocations")
                )
                == 0
            )
            assert (
                await session.scalar(
                    text(
                        "SELECT count(*) FROM github_user_repository_access "
                        "WHERE github_user_id=42 AND repository_external_id=101"
                    )
                )
                == 1
            )
            assert (
                await session.scalar(text("SELECT enabled FROM repositories WHERE external_id=101"))
                is True
            )
        retry = SyncInstallationRepositories(
            uow_factory=lambda: SqlAlchemyInstallationRepositoriesUnitOfWork(factory), rule_sets={}
        )
        await retry.disable(
            provider_installation_id=INSTALLATION_A,
            repositories=arguments,
            delivery_id="retry-after-rollback",
        )
        async with factory() as session:
            assert (
                await session.scalar(
                    text("SELECT count(*) FROM github_installation_removal_effects")
                )
                == 1
            )
            assert (
                await session.scalar(
                    text(
                        "SELECT count(*) FROM github_user_repository_access "
                        "WHERE github_user_id=42 AND repository_external_id=101"
                    )
                )
                == 0
            )
        await engine.dispose()

    asyncio.run(exercise())


@pytest.mark.integration
@pytest.mark.parametrize("commit_first", [False, True])
def test_concurrent_removal_marker_waits_for_first_commit_or_rollback(
    portal_database: tuple[str, str],
    commit_first: bool,
) -> None:
    from app.modules.repositories.infrastructure.installation_repository_unit_of_work import (
        SqlAlchemyInstallationRepositoriesUnitOfWork,
    )

    database_url, schema = portal_database
    engine = create_async_engine(
        database_url, connect_args={"options": f"-csearch_path={schema}"}, poolclass=NullPool
    )
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async def exercise() -> None:
        try:
            contender_pid: asyncio.Future[int] = asyncio.get_running_loop().create_future()

            async def duplicate() -> bool:
                async with SqlAlchemyInstallationRepositoriesUnitOfWork(factory) as second:
                    contender_pid.set_result(
                        await second.session.scalar(text("SELECT pg_backend_pid()"))
                    )
                    inserted = await second.repositories.record_removal_delivery(
                        "concurrent-removal"
                    )
                    if inserted:
                        await second.repositories.disable_repository(INSTALLATION_A, 101)
                    await second.commit()
                    return inserted

            async with SqlAlchemyInstallationRepositoriesUnitOfWork(factory) as first:
                assert (
                    await first.repositories.record_removal_delivery("concurrent-removal") is True
                )
                await first.repositories.disable_repository(INSTALLATION_A, 101)
                pending = asyncio.create_task(duplicate())
                try:
                    pid = await asyncio.wait_for(contender_pid, timeout=5)
                    await _wait_for_postgres_lock(engine, pid, pending)
                    if commit_first:
                        await first.commit()
                    else:
                        await first.rollback()
                    assert await asyncio.wait_for(pending, timeout=5) is (not commit_first)
                finally:
                    pending.cancel()
                    await asyncio.gather(pending, return_exceptions=True)
            async with factory() as session:
                assert (
                    await session.scalar(
                        text("SELECT count(*) FROM github_installation_removal_effects")
                    )
                    == 1
                )
                assert (
                    await session.scalar(
                        text(
                            "SELECT count(*) FROM github_user_repository_access "
                            "WHERE github_user_id=42 AND repository_external_id=101"
                        )
                    )
                    == 0
                )
        finally:
            await engine.dispose()

    asyncio.run(exercise())


async def _wait_for_postgres_lock[T](
    engine: AsyncEngine, pid: int, pending: asyncio.Task[T]
) -> None:
    """Observe an already connected contender, without mistaking scheduling for a lock."""
    async with engine.connect() as connection:
        connection = await connection.execution_options(isolation_level="AUTOCOMMIT")
        async with asyncio.timeout(5):
            while True:
                if pending.done():
                    await pending
                    pytest.fail("contender completed before waiting for the PostgreSQL lock")
                waiting = await connection.scalar(
                    text("SELECT wait_event_type = 'Lock' FROM pg_stat_activity WHERE pid = :pid"),
                    {"pid": pid},
                )
                if waiting:
                    return


@pytest.mark.integration
@pytest.mark.parametrize("removal_first", [True, False])
def test_uncommitted_removal_and_oauth_apply_serialize_in_both_orders(
    portal_database: tuple[str, str], removal_first: bool
) -> None:
    from app.modules.repositories.infrastructure.installation_repository_unit_of_work import (
        SqlAlchemyInstallationRepositoryStore,
    )
    from app.modules.workspaces.infrastructure.github_installation_links import (
        SqlAlchemyGitHubInstallationLinkStore,
    )

    database_url, schema = portal_database
    engine = create_async_engine(
        database_url, connect_args={"options": f"-csearch_path={schema}"}, poolclass=NullPool
    )
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async def exercise() -> None:
        pending: asyncio.Task[None] | None = None
        try:
            async with factory() as removal, factory() as oauth:
                try:
                    # Both connections exist before the contender. Snapshot predates removal.
                    removal_pid = await removal.scalar(text("SELECT pg_backend_pid()"))
                    oauth_pid = await oauth.scalar(text("SELECT pg_backend_pid()"))
                    snapshot_started_at = await oauth.scalar(text("SELECT clock_timestamp()"))
                    remover = SqlAlchemyInstallationRepositoryStore(removal)
                    links = SqlAlchemyGitHubInstallationLinkStore(oauth)
                    if removal_first:
                        await remover.disable_repository(INSTALLATION_A, 101)
                        pending = asyncio.create_task(
                            links.reconcile_repositories(42, 17, (101,), snapshot_started_at)
                        )
                        await _wait_for_postgres_lock(engine, oauth_pid, pending)
                        await removal.commit()
                        await asyncio.wait_for(pending, timeout=5)
                        await oauth.commit()
                        user_id, expected = 42, [103]
                    else:
                        # User43 has 102; removal must also see the new, uncommitted 101 grant.
                        await links.reconcile_repositories(43, 17, (101, 102), snapshot_started_at)
                        pending = asyncio.create_task(
                            remover.disable_repository(INSTALLATION_A, 101)
                        )
                        await _wait_for_postgres_lock(engine, removal_pid, pending)
                        await oauth.commit()
                        await asyncio.wait_for(pending, timeout=5)
                        await removal.commit()
                        user_id, expected = 43, [102]
                    async with factory() as observer:
                        grants = await observer.scalars(
                            text(
                                "SELECT repository_external_id FROM github_user_repository_access "
                                "WHERE github_user_id=:user ORDER BY repository_external_id"
                            ),
                            {"user": user_id},
                        )
                        assert list(grants) == expected
                finally:
                    if pending is not None:
                        pending.cancel()
                        await asyncio.gather(pending, return_exceptions=True)
        finally:
            await engine.dispose()

    asyncio.run(exercise())


@pytest.mark.integration
def test_removal_lock_allows_onboarding_foreign_key_check_without_deadlock(
    portal_database: tuple[str, str],
) -> None:
    from app.modules.repositories.application.installation_repositories import RepositorySnapshot
    from app.modules.repositories.infrastructure.installation_repository_unit_of_work import (
        SqlAlchemyInstallationRepositoryStore,
    )

    database_url, schema = portal_database
    engine = create_async_engine(
        database_url, connect_args={"options": f"-csearch_path={schema}"}, poolclass=NullPool
    )
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async def exercise() -> None:
        pending: asyncio.Task[None] | None = None
        try:
            async with factory() as onboarding, factory() as removal:
                try:
                    removal_pid = await removal.scalar(text("SELECT pg_backend_pid()"))
                    # Onboarding owns a repository row before requesting an installation FK.
                    await onboarding.execute(
                        text(
                            "UPDATE repositories SET full_name='octo/renamed' WHERE external_id=101"
                        )
                    )
                    pending = asyncio.create_task(
                        SqlAlchemyInstallationRepositoryStore(removal).disable_repository(
                            INSTALLATION_A, 101
                        )
                    )
                    await _wait_for_postgres_lock(engine, removal_pid, pending)
                    await asyncio.wait_for(
                        SqlAlchemyInstallationRepositoryStore(onboarding).upsert_repository(
                            INSTALLATION_A,
                            RepositorySnapshot(
                                104, "octo/new", "main", "https://github.test/octo/new"
                            ),
                        ),
                        timeout=5,
                    )
                    await onboarding.commit()
                    await asyncio.wait_for(pending, timeout=5)
                    await removal.commit()
                    async with factory() as observer:
                        assert (
                            await observer.scalar(
                                text("SELECT enabled FROM repositories WHERE external_id=101")
                            )
                            is False
                        )
                        assert (
                            await observer.scalar(
                                text("SELECT count(*) FROM repositories WHERE external_id=104")
                            )
                            == 1
                        )
                finally:
                    if pending is not None:
                        pending.cancel()
                        await asyncio.gather(pending, return_exceptions=True)
        finally:
            await engine.dispose()

    asyncio.run(exercise())
