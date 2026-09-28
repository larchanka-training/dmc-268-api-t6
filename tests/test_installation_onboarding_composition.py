"""Behavioural contract for the installation-event application composition."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4

import httpx
import pytest
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.schema import CreateSchema, DropSchema

from alembic import command
from app.bootstrap.installation_onboarding import InstallationOnboarding
from app.bootstrap.reviews_api import (
    ReviewsApiResources,
    get_github_webhook_receipt_uow_factory,
)
from app.main import (
    app,
    get_github_webhook_secret,
)
from app.modules.integrations.webhooks.application.installation_event_projector import (
    InstallationRepositoryTreeProvider,
)
from app.modules.integrations.webhooks.application.receive_github_delivery import (
    ReceiveGitHubDelivery,
)
from app.modules.integrations.webhooks.infrastructure.github_installation_tree_provider import (
    GitHubInstallationAccessTokenProvider,
)
from app.modules.repositories.application.installation_repositories import (
    InstallationRepositoriesEvent,
    RepositorySnapshot,
    RepositoryTreeBlob,
)
from app.modules.repositories.application.onboard_repository import (
    DefaultRuleSet,
    OnboardingResult,
    load_default_rule_sets,
)
from app.modules.repositories.application.sync_installation_repositories import (
    RepositoryOnboardingInput,
)
from app.modules.repositories.infrastructure.models import (
    ProviderInstallation,
    Repository,
    RuleVersion,
)
from app.modules.workspaces.infrastructure.models import Workspace


@dataclass
class FakeTreeProvider(InstallationRepositoryTreeProvider):
    calls: list[tuple[int, int]] = field(default_factory=list)

    async def fetch_default_branch_tree(
        self,
        *,
        installation_external_id: int,
        repository: RepositorySnapshot,
    ) -> tuple[RepositoryTreeBlob, ...]:
        self.calls.append((installation_external_id, repository.external_id))
        return (RepositoryTreeBlob(path="src/app.ts", size=10, entry_type="blob"),)


@pytest.fixture
def migrated_onboarding_database() -> Iterator[tuple[str, str]]:
    """Provide an isolated migrated PostgreSQL schema when configured."""
    database_url = os.environ.get("TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("set TEST_DATABASE_URL to run PostgreSQL integration tests")
    schema = f"test_installation_onboarding_{uuid4().hex}"
    engine = create_engine(database_url)
    try:
        with engine.connect() as connection:
            connection.execute(CreateSchema(schema))
            connection.execute(text(f'SET search_path TO "{schema}"'))
            connection.commit()
            config = Config("alembic.ini")
            config.attributes["connection"] = connection
            command.upgrade(config, "head")
            yield database_url, schema
            connection.rollback()
            connection.execute(text("SET search_path TO public"))
            connection.execute(DropSchema(schema, cascade=True))
            connection.commit()
    finally:
        engine.dispose()


def test_composition_accepts_typed_event_and_internal_installation_id(
    monkeypatch: Any,
) -> None:
    """The callable boundary sends a typed event through the composed projector."""
    provider = FakeTreeProvider()
    internal_installation_id = uuid4()
    observed: dict[str, object] = {}

    class FakeSync:
        def __init__(self, *, uow_factory: object, rule_sets: object) -> None:
            observed["rule_sets"] = rule_sets

        async def execute(
            self, *, provider_installation_id: UUID, repositories: object
        ) -> tuple[OnboardingResult, ...]:
            observed["provider_installation_id"] = provider_installation_id
            observed["repositories"] = repositories
            return ()

        async def disable(self, *, provider_installation_id: UUID, repositories: object) -> None:
            raise AssertionError("added event must not disable repositories")

    monkeypatch.setattr(
        "app.bootstrap.installation_onboarding.SyncInstallationRepositories", FakeSync
    )
    handler = InstallationOnboarding(
        session_factory=cast(async_sessionmaker[AsyncSession], object()),
        tree_provider=provider,
        rules_dir=Path("review/rules"),
    )

    asyncio.run(
        handler.execute(
            provider_installation_id=internal_installation_id,
            event=InstallationRepositoriesEvent(
                installation_external_id=17,
                action="added",
                added_repositories=(
                    RepositorySnapshot(
                        external_id=101,
                        full_name="octo/web",
                        default_branch="main",
                        web_url="https://github.com/octo/web",
                    ),
                ),
                removed_repositories=(),
            ),
        )
    )

    assert provider.calls == [(17, 101)]
    assert observed["provider_installation_id"] == internal_installation_id
    repositories = cast(tuple[RepositoryOnboardingInput, ...], observed["repositories"])
    rule_sets = cast(Mapping[str, DefaultRuleSet], observed["rule_sets"])
    assert repositories[0].languages == {"TypeScript": 100}
    assert set(rule_sets) == {"backend", "frontend"}


def test_resources_composes_onboarding_with_cwd_independent_default_rules(tmp_path: Path) -> None:
    """The running app's resource root exposes the real onboarding composition."""
    previous_directory = Path.cwd()
    os.chdir(tmp_path)
    try:
        provider = FakeTreeProvider()
        resources = ReviewsApiResources(
            cast(AsyncEngine, object()),
            cast(async_sessionmaker[AsyncSession], object()),
        )

        handler = resources.installation_onboarding(provider)

        result = asyncio.run(
            handler.execute(
                provider_installation_id=uuid4(),
                event=InstallationRepositoriesEvent(
                    installation_external_id=17,
                    action="created",
                    added_repositories=(),
                    removed_repositories=(),
                ),
            )
        )
    finally:
        os.chdir(previous_directory)

    assert result == ()
    assert provider.calls == []


@pytest.mark.integration
def test_migrated_database_onboarding_creates_one_active_rule_version_and_replay_preserves_it(
    migrated_onboarding_database: tuple[str, str],
) -> None:
    """The callable app boundary persists the language-selected initial version once."""
    database_url, schema = migrated_onboarding_database
    installation_id = uuid4()

    async def execute_and_read() -> tuple[Repository, list[RuleVersion]]:
        engine = create_async_engine(
            database_url,
            connect_args={"options": f"-csearch_path={schema}"},
        )
        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with session_factory() as session:
                workspace = Workspace(id=uuid4(), name="onboarding", daily_budget_usd=Decimal("1"))
                session.add(workspace)
                await session.flush()
                session.add(
                    ProviderInstallation(
                        id=installation_id,
                        workspace_id=workspace.id,
                        provider="github",
                        external_id=17,
                        provider_metadata={},
                    )
                )
                await session.commit()

            provider = FakeTreeProvider()
            handler = ReviewsApiResources(engine, session_factory).installation_onboarding(provider)
            event = InstallationRepositoriesEvent(
                installation_external_id=17,
                action="added",
                added_repositories=(
                    RepositorySnapshot(
                        external_id=101,
                        full_name="octo/web",
                        default_branch="main",
                        web_url="https://github.com/octo/web",
                    ),
                ),
                removed_repositories=(),
            )

            first = await handler.execute(provider_installation_id=installation_id, event=event)
            replay = await handler.execute(provider_installation_id=installation_id, event=event)
            assert first[0].created is True
            assert replay[0].created is False
            assert provider.calls == [(17, 101), (17, 101)]

            async with session_factory() as session:
                repository = await session.scalar(
                    select(Repository).where(
                        Repository.provider_installation_id == installation_id,
                        Repository.external_id == 101,
                    )
                )
                assert repository is not None
                versions = list(
                    (
                        await session.scalars(
                            select(RuleVersion)
                            .where(RuleVersion.repository_id == repository.id)
                            .order_by(RuleVersion.version)
                        )
                    ).all()
                )
                return repository, versions
        finally:
            await engine.dispose()

    repository, versions = asyncio.run(execute_and_read())

    assert repository.enabled is True
    assert len(versions) == 1
    assert versions[0].version == 1
    assert versions[0].is_active is True
    assert (
        versions[0].rules
        == load_default_rule_sets(Path(__file__).resolve().parents[1] / "review" / "rules")[
            "frontend"
        ].rules
    )


@pytest.mark.integration
def test_signed_runtime_delivery_onboards_replays_removes_and_ignores_unknown_installation(
    migrated_onboarding_database: tuple[str, str],
) -> None:
    """The endpoint uses real resolver/onboarding while GitHub I/O stays injectable."""
    database_url, schema = migrated_onboarding_database
    installation_id = uuid4()
    secret = "runtime-webhook-secret"
    tree_requests: list[str] = []

    @dataclass
    class TokenProvider(GitHubInstallationAccessTokenProvider):
        async def get_installation_access_token(self, installation_external_id: int) -> str:
            assert installation_external_id == 17
            return "test-installation-token"

    def handler(request: httpx.Request) -> httpx.Response:
        tree_requests.append(str(request.url))
        return httpx.Response(200, json={"tree": [{"path": "src/app.ts", "type": "blob"}]})

    async def exercise() -> tuple[Repository | None, list[RuleVersion], int]:
        engine = create_async_engine(
            database_url, connect_args={"options": f"-csearch_path={schema}"}
        )
        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url="https://api.github.com"
        )
        try:
            async with session_factory() as session:
                workspace = Workspace(id=uuid4(), name="runtime", daily_budget_usd=Decimal("1"))
                session.add(workspace)
                await session.flush()
                session.add(
                    ProviderInstallation(
                        id=installation_id,
                        workspace_id=workspace.id,
                        provider="github",
                        external_id=17,
                        provider_metadata={},
                    )
                )
                await session.commit()

            dispatcher = ReviewsApiResources(
                engine, session_factory
            ).github_installation_delivery_dispatcher(client=client, token_provider=TokenProvider())
            receiver = ReceiveGitHubDelivery(
                uow_factory=ReviewsApiResources(engine, session_factory).github_webhook_receipts,
                dispatcher=dispatcher,
            )
            app.dependency_overrides[get_github_webhook_secret] = lambda: secret
            app.dependency_overrides[get_github_webhook_receipt_uow_factory] = lambda: (
                ReviewsApiResources(engine, session_factory).github_webhook_receipts
            )

            async def post(payload: Mapping[str, object], delivery: str) -> int:
                raw_body = json.dumps(payload).encode()
                signature = hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
                with TestClient(app) as test_client:
                    response = test_client.post(
                        "/webhooks/github",
                        content=raw_body,
                        headers={
                            "X-GitHub-Event": "installation_repositories",
                            "X-GitHub-Delivery": delivery,
                            "X-Hub-Signature-256": f"sha256={signature}",
                        },
                    )
                assert response.status_code == 202
                await receiver.replay_pending()
                return len(tree_requests)

            added: dict[str, object] = {
                "action": "added",
                "installation": {"id": 17},
                "repositories_added": [
                    {
                        "id": 101,
                        "full_name": "octo/web",
                        "default_branch": "main",
                        "html_url": "https://github.com/octo/web",
                    }
                ],
                "repositories_removed": [],
            }
            assert await post(added, "delivery-added") == 1
            assert await post(added, "delivery-replay") == 2
            removed: dict[str, object] = {
                **added,
                "action": "removed",
                "repositories_added": [],
                "repositories_removed": added["repositories_added"],
            }
            assert await post(removed, "delivery-removed") == 2
            unknown = {**added, "installation": {"id": 999}}
            assert await post(unknown, "delivery-unknown") == 2

            async with session_factory() as session:
                repository = await session.scalar(
                    select(Repository).where(Repository.external_id == 101)
                )
                versions = list((await session.scalars(select(RuleVersion))).all())
            return repository, versions, len(tree_requests)
        finally:
            app.dependency_overrides.clear()
            await client.aclose()
            await engine.dispose()

    repository, versions, tree_fetches = asyncio.run(exercise())

    assert repository is not None
    assert repository.enabled is False
    assert len(versions) == 1
    assert versions[0].version == 1
    assert versions[0].is_active is True
    assert tree_fetches == 2
