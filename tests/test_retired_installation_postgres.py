"""Durable installation retirement, provider refusal and positive-write races."""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.bootstrap.installation_onboarding import InstallationOnboarding
from app.modules.integrations.webhooks.api.dispatch import GitHubWebhookDispatchAdapter
from app.modules.integrations.webhooks.api.receipt import VerifiedGitHubDelivery
from app.modules.integrations.webhooks.application.github_installation_dispatch import (
    GitHubInstallationDeliveryDispatcher,
    InstallationDeliveryDispatchStatus,
)
from app.modules.integrations.webhooks.application.installation_event_projector import (
    InstallationProviderCheckpoint,
    RepositoryDetails,
)
from app.modules.integrations.webhooks.application.receive_github_delivery import (
    ReceiveGitHubDelivery,
)
from app.modules.integrations.webhooks.infrastructure.github_installation_resolver import (
    SqlAlchemyGitHubInstallationResolver,
)
from app.modules.integrations.webhooks.infrastructure.github_installation_tree_provider import (
    GitHubAppInstallationAccessTokenProvider,
    GitHubInstallationTreeProvider,
)
from app.modules.integrations.webhooks.infrastructure.github_repository_details import (
    GitHubInstallationRepositoryDetailsProvider,
)
from app.modules.integrations.webhooks.infrastructure.github_repository_labels import (
    GitHubRepositoryLabelProvider,
)
from app.modules.integrations.webhooks.infrastructure.github_webhook_receipts import (
    SqlAlchemyGitHubWebhookReceiptUnitOfWork,
)
from app.modules.repositories.application.installation_repositories import (
    InstallationRepositoriesEvent,
    RepositoryReference,
    RepositorySnapshot,
    RepositoryTreeBlob,
)
from app.modules.repositories.application.onboard_repository import load_default_rule_sets
from app.modules.repositories.application.sync_installation_repositories import (
    RepositoryOnboardingInput,
    SyncInstallationRepositories,
)
from app.modules.repositories.infrastructure.installation_repository_unit_of_work import (
    SqlAlchemyInstallationRepositoriesUnitOfWork,
)
from tests.portal_postgres import INSTALL_A, REPO_A, RULE_A, WS_A, Env, portal_schema
from tests.test_app_reinstall_postgres import FreshGitHub, apply, seed_archive


@pytest.fixture
def retired_env() -> Iterator[Env]:
    database_url = os.environ.get("TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("set TEST_DATABASE_URL to run PostgreSQL integration tests")
    with portal_schema(database_url, None) as env:
        yield env


@pytest.mark.integration
@pytest.mark.parametrize("preference", [False, True])
def test_whole_deletion_commits_permanent_marker_without_destroying_preference_or_history(
    retired_env: Env,
    preference: bool,
) -> None:
    async def exercise() -> None:
        engine = retired_env.engine()
        factory = async_sessionmaker(engine, expire_on_commit=False)
        sync = SyncInstallationRepositories(
            uow_factory=lambda: SqlAlchemyInstallationRepositoriesUnitOfWork(factory),
            rule_sets=load_default_rule_sets(Path("review/rules")),
        )
        try:
            async with factory.begin() as session:
                await session.execute(
                    text("UPDATE repositories SET enabled=:enabled WHERE id=:repo"),
                    {"enabled": preference, "repo": REPO_A},
                )
            await sync.disable(
                provider_installation_id=INSTALL_A,
                repositories=(),
                delivery_id="113-deletion",
                all_repositories=True,
            )
            async with factory() as session:
                assert (
                    await session.scalar(
                        text(
                            "SELECT deleted_at IS NOT NULL FROM provider_installations WHERE id=:id"
                        ),
                        {"id": INSTALL_A},
                    )
                    is True
                )
                row = (
                    await session.execute(
                        text(
                            "SELECT enabled,installation_available FROM repositories WHERE id=:repo"
                        ),
                        {"repo": REPO_A},
                    )
                ).one()
                assert tuple(row) == (preference, False)
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
                assert await session.scalar(text("SELECT count(*) FROM runs")) == 4
                assert (
                    await session.scalar(
                        text(
                            "SELECT count(*) FROM github_installation_removal_effects WHERE "
                            "delivery_id='113-deletion'"
                        )
                    )
                    == 1
                )
        finally:
            await engine.dispose()

    asyncio.run(exercise())


@dataclass
class Providers:
    factory: async_sessionmaker[AsyncSession]
    calls: list[str] = field(default_factory=list)

    async def unlocked_call(self, name: str) -> None:
        # A provider boundary must be entered after the preflight UOW releases its lock.
        async with self.factory.begin() as session:
            await session.execute(
                text("SELECT id FROM provider_installations WHERE id=:id FOR UPDATE NOWAIT"),
                {"id": INSTALL_A},
            )
        self.calls.append(name)

    async def fetch_repository_details(
        self,
        *,
        checkpoint: InstallationProviderCheckpoint,
        installation_external_id: int,
        full_name: str,
    ) -> RepositoryDetails:
        await self.unlocked_call("details")
        return RepositoryDetails("main", "https://github.test/octo/new")

    async def fetch_default_branch_tree(
        self,
        *,
        checkpoint: InstallationProviderCheckpoint,
        installation_external_id: int,
        repository: RepositorySnapshot,
    ) -> tuple[RepositoryTreeBlob, ...]:
        await self.unlocked_call("tree")
        return (RepositoryTreeBlob("src/a.py", 10, "blob"),)

    async def create_ai_review_label(
        self,
        *,
        checkpoint: InstallationProviderCheckpoint,
        installation_external_id: int,
        repository: RepositorySnapshot,
    ) -> None:
        await self.unlocked_call("label")


@pytest.mark.integration
@pytest.mark.parametrize(
    ("action", "retired", "expected_calls", "repo_count"),
    [
        ("created", True, [], 2),
        ("added", True, [], 2),
        ("created", False, ["details", "tree", "label"], 3),
    ],
)
def test_retired_positive_delivery_makes_no_provider_calls_or_repository_writes(
    retired_env: Env,
    action: str,
    retired: bool,
    expected_calls: list[str],
    repo_count: int,
) -> None:
    async def exercise() -> None:
        engine = retired_env.engine()
        factory = async_sessionmaker(engine, expire_on_commit=False)
        providers = Providers(factory)
        sync = SyncInstallationRepositories(
            uow_factory=lambda: SqlAlchemyInstallationRepositoriesUnitOfWork(factory),
            rule_sets=load_default_rule_sets(Path("review/rules")),
        )
        try:
            if retired:
                await sync.disable(
                    provider_installation_id=INSTALL_A,
                    repositories=(),
                    delivery_id="113-retire",
                    all_repositories=True,
                )
            handler = InstallationOnboarding(
                session_factory=factory,
                tree_provider=providers,
                label_provider=providers,
                details_provider=providers,
            )
            event = InstallationRepositoriesEvent(
                17,
                "created" if action == "created" else "added",
                (RepositoryReference(103, "octo/new", None, None),),
                (),
            )
            results = await handler.execute(provider_installation_id=INSTALL_A, event=event)
            assert providers.calls == expected_calls
            assert len(results) == (0 if retired else 1)
            async with factory() as session:
                assert await session.scalar(text("SELECT count(*) FROM repositories")) == repo_count
                assert (
                    await session.scalar(text("SELECT count(*) FROM rule_versions")) == repo_count
                )
                assert await session.scalar(text("SELECT count(*) FROM runs")) == 4
        finally:
            await engine.dispose()

    asyncio.run(exercise())


@pytest.mark.integration
def test_positive_sync_rechecks_retirement_inside_write_transaction(retired_env: Env) -> None:
    async def exercise() -> None:
        engine = retired_env.engine()
        factory = async_sessionmaker(engine, expire_on_commit=False)
        sync = SyncInstallationRepositories(
            uow_factory=lambda: SqlAlchemyInstallationRepositoriesUnitOfWork(factory),
            rule_sets=load_default_rule_sets(Path("review/rules")),
        )
        try:
            assert await sync.is_active(INSTALL_A) is True
            await sync.disable(
                provider_installation_id=INSTALL_A,
                repositories=(),
                delivery_id="113-before-sync",
                all_repositories=True,
            )
            results = await sync.execute(
                provider_installation_id=INSTALL_A,
                repositories=(
                    RepositoryOnboardingInput(
                        RepositorySnapshot(103, "octo/new", "main", "https://github.test/octo/new"),
                        {},
                    ),
                ),
            )
            assert results == ()
            async with factory() as session:
                assert await session.scalar(text("SELECT count(*) FROM repositories")) == 2
                assert await session.scalar(text("SELECT count(*) FROM rule_versions")) == 2
                assert (
                    await session.scalar(
                        text("SELECT installation_available FROM repositories WHERE id=:repo"),
                        {"repo": REPO_A},
                    )
                    is False
                )
        finally:
            await engine.dispose()

    asyncio.run(exercise())


@pytest.mark.integration
def test_retirement_during_tree_fetch_prevents_later_label_call_and_positive_write(
    retired_env: Env,
) -> None:
    async def exercise() -> None:
        engine = retired_env.engine()
        factory = async_sessionmaker(engine, expire_on_commit=False)
        started = asyncio.Event()
        release = asyncio.Event()

        class InFlightProviders(Providers):
            async def fetch_default_branch_tree(
                self,
                *,
                checkpoint: InstallationProviderCheckpoint,
                installation_external_id: int,
                repository: RepositorySnapshot,
            ) -> tuple[RepositoryTreeBlob, ...]:
                await self.unlocked_call("tree")
                started.set()
                await release.wait()
                return (RepositoryTreeBlob("src/a.py", 10, "blob"),)

        providers = InFlightProviders(factory)
        sync = SyncInstallationRepositories(
            uow_factory=lambda: SqlAlchemyInstallationRepositoriesUnitOfWork(factory),
            rule_sets=load_default_rule_sets(Path("review/rules")),
        )
        try:
            handler = InstallationOnboarding(
                session_factory=factory,
                tree_provider=providers,
                label_provider=providers,
                details_provider=providers,
            )
            event = InstallationRepositoriesEvent(
                17, "created", (RepositoryReference(103, "octo/new", None, None),), ()
            )
            onboarding = asyncio.create_task(
                handler.execute(provider_installation_id=INSTALL_A, event=event)
            )
            await asyncio.wait_for(started.wait(), timeout=2)
            await sync.disable(
                provider_installation_id=INSTALL_A,
                repositories=(),
                delivery_id="113-inflight-delete",
                all_repositories=True,
            )
            release.set()
            assert await asyncio.wait_for(onboarding, timeout=5) == ()
            assert providers.calls == ["details", "tree"]
            async with factory() as session:
                assert await session.scalar(text("SELECT count(*) FROM repositories")) == 2
                assert await session.scalar(text("SELECT count(*) FROM rule_versions")) == 2
                assert (
                    await session.scalar(
                        text("SELECT installation_available FROM repositories WHERE id=:repo"),
                        {"repo": REPO_A},
                    )
                    is False
                )
        finally:
            release.set()
            await engine.dispose()

    asyncio.run(exercise())


@pytest.mark.integration
@pytest.mark.parametrize("whole_old_removal", [False, True])
def test_old_source_removal_and_duplicate_do_not_revoke_rebound_target(
    retired_env: Env,
    whole_old_removal: bool,
) -> None:
    async def exercise() -> None:
        engine = retired_env.engine()
        factory = async_sessionmaker(engine, expire_on_commit=False)
        sync = SyncInstallationRepositories(
            uow_factory=lambda: SqlAlchemyInstallationRepositoriesUnitOfWork(factory),
            rule_sets=load_default_rule_sets(Path("review/rules")),
        )
        try:
            async with factory.begin() as session:
                await session.execute(
                    text("UPDATE provider_installations SET account_external_id=700 WHERE id=:id"),
                    {"id": INSTALL_A},
                )
                await session.execute(
                    text("UPDATE repositories SET enabled=false WHERE id=:repo"), {"repo": REPO_A}
                )
                await session.execute(
                    text("UPDATE runs SET state='succeeded' WHERE state='queued'")
                )
            await sync.disable(
                provider_installation_id=INSTALL_A,
                repositories=(),
                delivery_id="113-original",
                all_repositories=True,
            )
            async with factory() as session:
                retired_at = await session.scalar(
                    text("SELECT deleted_at FROM provider_installations WHERE id=:id"),
                    {"id": INSTALL_A},
                )
            assert await apply(factory, FreshGitHub()) == (WS_A,)
            await sync.disable(
                provider_installation_id=INSTALL_A,
                repositories=(),
                delivery_id="113-original",
                all_repositories=True,
            )
            await sync.disable(
                provider_installation_id=INSTALL_A,
                repositories=(RepositoryReference(101, "octo/a", None, None),),
                delivery_id="113-distinct-old",
                all_repositories=whole_old_removal,
            )
            providers = Providers(factory)
            onboarding = InstallationOnboarding(
                session_factory=factory,
                tree_provider=providers,
                label_provider=providers,
                details_provider=providers,
            )
            assert (
                await onboarding.execute(
                    provider_installation_id=INSTALL_A,
                    event=InstallationRepositoriesEvent(
                        17, "created", (RepositoryReference(101, "octo/a", None, None),), ()
                    ),
                )
                == ()
            )
            assert providers.calls == []
            async with factory() as session:
                row = (
                    await session.execute(
                        text(
                            "SELECT i.external_id,r.enabled,r.installation_available FROM "
                            "repositories r "
                            "JOIN provider_installations i ON i.id=r.provider_installation_id WHERE"
                            " r.id=:repo"
                        ),
                        {"repo": REPO_A},
                    )
                ).one()
                assert tuple(row) == (29, False, True)
                assert (
                    await session.scalar(
                        text(
                            "SELECT count(*) FROM github_user_repository_access a "
                            "JOIN provider_installations i ON i.id=a.provider_installation_id "
                            "WHERE i.external_id=29 AND a.github_user_id=42 AND "
                            "repository_external_id=101"
                        )
                    )
                    == 1
                )
                assert (
                    await session.scalar(
                        text("SELECT deleted_at FROM provider_installations WHERE id=:id"),
                        {"id": INSTALL_A},
                    )
                    == retired_at
                )
                assert (
                    await session.scalar(
                        text("SELECT count(*) FROM github_installation_removal_effects")
                    )
                    == 2
                )
        finally:
            await engine.dispose()

    asyncio.run(exercise())


@pytest.mark.integration
def test_new_installation_created_before_oauth_is_deferred_then_resumes_with_saved_pause_and_rules(
    retired_env: Env,
) -> None:
    async def exercise() -> None:
        engine = retired_env.engine()
        factory = async_sessionmaker(engine, expire_on_commit=False)
        providers = Providers(factory)
        receiver = ReceiveGitHubDelivery(
            uow_factory=lambda: SqlAlchemyGitHubWebhookReceiptUnitOfWork(factory),
            dispatcher=GitHubWebhookDispatchAdapter(
                GitHubInstallationDeliveryDispatcher(
                    resolver=SqlAlchemyGitHubInstallationResolver(factory),
                    onboarding=InstallationOnboarding(
                        session_factory=factory,
                        tree_provider=providers,
                        label_provider=providers,
                        details_provider=providers,
                    ),
                )
            ),
        )
        try:
            await seed_archive(factory)
            async with factory() as session:
                saved_rule = (
                    await session.execute(
                        text("SELECT rules,checksum,is_active FROM rule_versions WHERE id=:id"),
                        {"id": RULE_A},
                    )
                ).one()
            receipt = VerifiedGitHubDelivery(
                "113-created-before-oauth",
                "installation",
                {
                    "action": "created",
                    "installation": {"id": 29},
                    "repositories": [{"id": 101, "full_name": "octo/a"}],
                },
            ).to_receipt()
            assert await receiver.execute(receipt) is InstallationDeliveryDispatchStatus.PENDING
            assert await receiver.replay_pending() == 1
            assert providers.calls == []
            async with factory() as session:
                deferred = (
                    await session.execute(
                        text(
                            "SELECT projected_at IS NULL,retry_after IS NOT NULL,"
                            "projection_attempt_count FROM webhook_events WHERE delivery_id=:id"
                        ),
                        {"id": receipt.delivery_id},
                    )
                ).one()
                assert tuple(deferred) == (True, True, 1)
                assert (
                    await session.scalar(
                        text("SELECT count(*) FROM provider_installations WHERE external_id=29")
                    )
                    == 0
                )
                archived = (
                    await session.execute(
                        text(
                            "SELECT provider_installation_id,enabled,installation_available "
                            "FROM repositories WHERE id=:repo"
                        ),
                        {"repo": REPO_A},
                    )
                ).one()
                assert tuple(archived) == (INSTALL_A, False, False)
            await apply(factory, FreshGitHub())
            async with factory() as session:
                woken = (
                    await session.execute(
                        text(
                            "SELECT projected_at IS NULL,retry_after IS NULL,"
                            "projection_attempt_count FROM webhook_events WHERE delivery_id=:id"
                        ),
                        {"id": receipt.delivery_id},
                    )
                ).one()
                assert tuple(woken) == (True, True, 0)
            assert await receiver.replay_pending() == 1
            assert providers.calls == ["details", "tree", "label"]
            async with factory() as session:
                assert (
                    await session.scalar(
                        text(
                            "SELECT projected_at IS NOT NULL FROM webhook_events "
                            "WHERE delivery_id=:id"
                        ),
                        {"id": receipt.delivery_id},
                    )
                    is True
                )
                restored = (
                    await session.execute(
                        text(
                            "SELECT pi.external_id,r.enabled,r.installation_available,"
                            "rv.id,r.max_comments,r.wait_for_ci "
                            "FROM repositories r JOIN provider_installations pi "
                            "ON pi.id=r.provider_installation_id JOIN rule_versions rv "
                            "ON rv.repository_id=r.id AND rv.is_active WHERE r.id=:repo"
                        ),
                        {"repo": REPO_A},
                    )
                ).one()
                assert tuple(restored) == (29, False, True, RULE_A, 7, "always")
                current_rule = (
                    await session.execute(
                        text("SELECT rules,checksum,is_active FROM rule_versions WHERE id=:id"),
                        {"id": RULE_A},
                    )
                ).one()
                assert current_rule == saved_rule
                assert await session.scalar(text("SELECT count(*) FROM runs")) == 4
        finally:
            await engine.dispose()

    asyncio.run(exercise())


class DiscardTokenCache:
    """Each phase mints a token so the inner HTTP boundary is exercised."""

    def get(self, installation_external_id: int) -> None:
        return None

    def set(self, installation_external_id: int, access_token: str, expires_at: datetime) -> None:
        pass


@pytest.mark.integration
@pytest.mark.parametrize("phase", ["details", "tree", "label", "pacing"])
@pytest.mark.parametrize("retire", [False, True])
def test_onboarding_stops_next_http_after_token_or_pacing_retirement(
    retired_env: Env,
    phase: str,
    retire: bool,
) -> None:
    async def exercise() -> None:
        engine = retired_env.engine()
        factory = async_sessionmaker(engine, expire_on_commit=False)
        sync = SyncInstallationRepositories(
            uow_factory=lambda: SqlAlchemyInstallationRepositoriesUnitOfWork(factory),
            rule_sets=load_default_rule_sets(Path("review/rules")),
        )
        calls: list[str] = []
        token_count = 0

        async def unlocked() -> None:
            async with factory.begin() as session:
                await session.execute(
                    text("SELECT id FROM provider_installations WHERE id=:id FOR UPDATE NOWAIT"),
                    {"id": INSTALL_A},
                )

        async def retire_source() -> None:
            await sync.disable(
                provider_installation_id=INSTALL_A,
                repositories=(),
                delivery_id=f"113-inner-{phase}",
                all_repositories=True,
            )

        async def http(request: httpx.Request) -> httpx.Response:
            nonlocal token_count
            await unlocked()
            path = request.url.path
            if path.endswith("/access_tokens"):
                token_count += 1
                calls.append("token")
                if retire and phase != "pacing" and token_count == (2 if phase == "label" else 1):
                    # This HTTP request started live and finishes after deletion commits.
                    await retire_source()
                return httpx.Response(
                    201, json={"token": "token", "expires_at": "2030-01-01T00:00:00Z"}
                )
            if path.endswith("/git/trees/main"):
                calls.append("tree")
                return httpx.Response(
                    200, json={"tree": [{"path": "src/a.py", "type": "blob", "size": 10}]}
                )
            if path.endswith("/labels"):
                calls.append("label")
                return httpx.Response(201, json={})
            calls.append("details")
            assert path == "/repos/octo/new"
            return httpx.Response(
                200, json={"default_branch": "main", "html_url": "https://github.test/octo/new"}
            )

        async def pacing(delay: float) -> None:
            await unlocked()
            if retire and phase == "pacing":
                await retire_source()

        try:
            async with httpx.AsyncClient(
                base_url="https://api.github.test", transport=httpx.MockTransport(http)
            ) as client:
                tokens = GitHubAppInstallationAccessTokenProvider(
                    client=client,
                    app_id="42",
                    private_key="unused",
                    jwt_encoder=lambda claims, key: "jwt",
                    cache=DiscardTokenCache(),
                    now=time.time,
                )
                onboarding = InstallationOnboarding(
                    session_factory=factory,
                    details_provider=GitHubInstallationRepositoryDetailsProvider(
                        client=client, token_provider=tokens
                    ),
                    tree_provider=GitHubInstallationTreeProvider(
                        client=client, token_provider=tokens
                    ),
                    label_provider=GitHubRepositoryLabelProvider(
                        client=client, token_provider=tokens, sleep=pacing
                    ),
                )
                reference = RepositoryReference(
                    103,
                    "octo/new",
                    None if phase == "details" else "main",
                    None if phase == "details" else "https://github.test/octo/new",
                )
                results = await onboarding.execute(
                    provider_installation_id=INSTALL_A,
                    event=InstallationRepositoriesEvent(17, "created", (reference,), ()),
                )
            expected = {
                "details": ["token"]
                if retire
                else ["token", "details", "token", "tree", "token", "label"],
                "tree": ["token"] if retire else ["token", "tree", "token", "label"],
                "label": ["token", "tree", "token"]
                if retire
                else ["token", "tree", "token", "label"],
                "pacing": ["token", "tree", "token"]
                if retire
                else ["token", "tree", "token", "label"],
            }
            assert calls == expected[phase]
            assert len(results) == (0 if retire else 1)
            async with factory() as session:
                assert await session.scalar(text("SELECT count(*) FROM repositories")) == (
                    2 if retire else 3
                )
                assert await session.scalar(text("SELECT count(*) FROM rule_versions")) == (
                    2 if retire else 3
                )
                assert await session.scalar(text("SELECT count(*) FROM runs")) == 4
        finally:
            await engine.dispose()

    asyncio.run(exercise())
