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
from app.modules.integrations.webhooks.api.receipt import VerifiedGitHubDelivery
from app.modules.integrations.webhooks.application.github_installation_dispatch import (
    InstallationDeliveryDispatchStatus,
)
from app.modules.integrations.webhooks.application.installation_event_projector import (
    InstallationRepositoryDetailsProvider,
    InstallationRepositoryLabelProvider,
    InstallationRepositoryTreeProvider,
    RepositoryDetails,
)
from app.modules.integrations.webhooks.application.receive_github_delivery import (
    ReceiveGitHubDelivery,
)
from app.modules.integrations.webhooks.infrastructure.github_installation_tree_provider import (
    GitHubInstallationAccessTokenProvider,
)
from app.modules.repositories.application.installation_repositories import (
    InstallationRepositoriesEvent,
    RepositoryReference,
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
from tests.github_webhook_fixtures import load_github_webhook_fixture, removal_of


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


@dataclass
class FakeLabelProvider(InstallationRepositoryLabelProvider):
    calls: list[tuple[int, str]] = field(default_factory=list)

    async def create_ai_review_label(
        self, *, installation_external_id: int, repository: RepositorySnapshot
    ) -> None:
        self.calls.append((installation_external_id, repository.full_name))


class UnusedDetailsProvider(InstallationRepositoryDetailsProvider):
    """Events in these tests carry branch and URL, so GitHub must not be asked for them."""

    async def fetch_repository_details(
        self, *, installation_external_id: int, full_name: str
    ) -> RepositoryDetails:
        raise AssertionError("complete repositories need no details request")


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
    labels = FakeLabelProvider()
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

        async def disable(
            self,
            *,
            provider_installation_id: UUID,
            repositories: object,
            delivery_id: str,
            all_repositories: bool = False,
        ) -> None:
            raise AssertionError("added event must not disable repositories")

    monkeypatch.setattr(
        "app.bootstrap.installation_onboarding.SyncInstallationRepositories", FakeSync
    )
    handler = InstallationOnboarding(
        session_factory=cast(async_sessionmaker[AsyncSession], object()),
        tree_provider=provider,
        label_provider=labels,
        details_provider=UnusedDetailsProvider(),
        rules_dir=Path("review/rules"),
    )

    asyncio.run(
        handler.execute(
            provider_installation_id=internal_installation_id,
            event=InstallationRepositoriesEvent(
                installation_external_id=17,
                action="added",
                added_repositories=(
                    RepositoryReference(
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
    assert labels.calls == [(17, "octo/web")]
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

        handler = resources.installation_onboarding(
            provider, FakeLabelProvider(), UnusedDetailsProvider()
        )

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


def test_runtime_dispatcher_creates_label_only_for_linked_added_repositories(
    monkeypatch: Any,
) -> None:
    requests: list[httpx.Request] = []
    synchronized: list[str] = []

    class Resolver:
        def __init__(self, session_factory: object) -> None:
            pass

        async def find_github_installation_id(self, external_id: int) -> UUID | None:
            return uuid4() if external_id == 17 else None

    class Sync:
        def __init__(self, *, uow_factory: object, rule_sets: object) -> None:
            pass

        async def execute(
            self, *, provider_installation_id: UUID, repositories: object
        ) -> tuple[OnboardingResult, ...]:
            assert [request.method for request in requests] == ["GET", "POST"]
            synchronized.append("added")
            return ()

        async def disable(
            self,
            *,
            provider_installation_id: UUID,
            repositories: object,
            delivery_id: str,
            all_repositories: bool = False,
        ) -> None:
            synchronized.append("removed")

    class TokenProvider:
        async def get_installation_access_token(self, installation_external_id: int) -> str:
            assert installation_external_id == 17
            return "installation-token"

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "GET":
            return httpx.Response(200, request=request, json={"tree": []})
        return httpx.Response(201, request=request, json={})

    monkeypatch.setattr("app.bootstrap.reviews_api.SqlAlchemyGitHubInstallationResolver", Resolver)
    monkeypatch.setattr("app.bootstrap.installation_onboarding.SyncInstallationRepositories", Sync)
    repository = {
        "id": 101,
        "full_name": "octo/api",
        "default_branch": "main",
        "html_url": "https://github.com/octo/api",
    }

    async def exercise() -> tuple[InstallationDeliveryDispatchStatus, ...]:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url="https://api.github.com"
        ) as client:
            resources = ReviewsApiResources(
                cast(AsyncEngine, object()), cast(async_sessionmaker[AsyncSession], object())
            )
            dispatcher = resources.github_installation_delivery_dispatcher(
                client=client, token_provider=TokenProvider()
            )
            results = []
            for installation_id, action, added, removed in (
                (17, "added", [repository], []),
                (999, "added", [repository], []),
                (17, "removed", [], [repository]),
            ):
                payload = {
                    "action": action,
                    "installation": {"id": installation_id},
                    "repositories_added": added,
                    "repositories_removed": removed,
                }
                result = await dispatcher.execute(
                    VerifiedGitHubDelivery(
                        "delivery", "installation_repositories", payload
                    ).to_receipt()
                )
                results.append(result.status)
            return tuple(results)

    statuses = asyncio.run(exercise())

    assert statuses == (
        InstallationDeliveryDispatchStatus.ONBOARDED,
        InstallationDeliveryDispatchStatus.IGNORED_UNKNOWN_INSTALLATION,
        InstallationDeliveryDispatchStatus.ONBOARDED,
    )
    assert [request.method for request in requests] == ["GET", "POST"]
    assert str(requests[1].url) == "https://api.github.com/repos/octo/api/labels"
    assert synchronized == ["added", "removed"]


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
            handler = ReviewsApiResources(engine, session_factory).installation_onboarding(
                provider, FakeLabelProvider(), UnusedDetailsProvider()
            )
            event = InstallationRepositoriesEvent(
                installation_external_id=17,
                action="added",
                added_repositories=(
                    RepositoryReference(
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
    label_requests: list[str] = []

    @dataclass
    class TokenProvider(GitHubInstallationAccessTokenProvider):
        async def get_installation_access_token(self, installation_external_id: int) -> str:
            assert installation_external_id == 17
            return "test-installation-token"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            label_requests.append(str(request.url))
            return httpx.Response(201, json={})
        tree_requests.append(str(request.url))
        return httpx.Response(200, json={"tree": [{"path": "src/app.ts", "type": "blob"}]})

    async def exercise() -> tuple[Repository | None, list[RuleVersion], int, int]:
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
            return repository, versions, len(tree_requests), len(label_requests)
        finally:
            app.dependency_overrides.clear()
            await client.aclose()
            await engine.dispose()

    repository, versions, tree_fetches, label_creations = asyncio.run(exercise())

    assert repository is not None
    assert repository.enabled is False
    assert len(versions) == 1
    assert versions[0].version == 1
    assert versions[0].is_active is True
    assert tree_fetches == 2
    assert label_creations == 2


@pytest.mark.integration
@pytest.mark.parametrize(
    ("event_name", "fixture", "external_id", "full_name"),
    [
        ("installation", "installation_created", 1000004, "example-owner/example-repo"),
        (
            "installation_repositories",
            "installation_repositories_added",
            1000005,
            "example-owner/example-repo-two",
        ),
    ],
)
def test_real_delivery_fixture_is_onboarded_with_branch_and_url_read_from_github(
    migrated_onboarding_database: tuple[str, str],
    event_name: str,
    fixture: str,
    external_id: int,
    full_name: str,
) -> None:
    """A delivery in the shape GitHub sends (api#71) reaches ``repositories``.

    The event carries neither ``default_branch`` nor ``html_url``: the row must hold
    the values of the stubbed ``GET /repos/{full_name}``, and the tree request must
    use that branch, proving the details were fetched before the tree.
    """
    database_url, schema = migrated_onboarding_database
    installation_id = uuid4()
    repository_path = f"/repos/{full_name}"
    requests: list[tuple[str, str]] = []

    class TokenProvider:
        async def get_installation_access_token(self, installation_external_id: int) -> str:
            assert installation_external_id == 1000001
            return "test-installation-token"

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append((request.method, request.url.path))
        if request.method == "GET" and request.url.path == repository_path:
            return httpx.Response(
                200,
                json={
                    "default_branch": "trunk",
                    "html_url": f"https://example.test/{full_name}",
                },
            )
        if request.method == "GET" and request.url.path == f"{repository_path}/git/trees/trunk":
            return httpx.Response(200, json={"tree": [{"path": "src/app.ts", "type": "blob"}]})
        if request.method == "POST" and request.url.path == f"{repository_path}/labels":
            return httpx.Response(201, json={})
        return httpx.Response(500, json={"message": "unexpected request"})

    async def exercise() -> tuple[
        InstallationDeliveryDispatchStatus,
        Repository | None,
        list[tuple[str, str]],
        InstallationDeliveryDispatchStatus,
        Repository | None,
        list[tuple[str, str]],
    ]:
        engine = create_async_engine(
            database_url, connect_args={"options": f"-csearch_path={schema}"}
        )
        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url="https://api.github.com"
        )
        try:
            async with session_factory() as session:
                workspace = Workspace(
                    id=uuid4(), name="real-payload", daily_budget_usd=Decimal("1")
                )
                session.add(workspace)
                await session.flush()
                session.add(
                    ProviderInstallation(
                        id=installation_id,
                        workspace_id=workspace.id,
                        provider="github",
                        external_id=1000001,
                        provider_metadata={},
                    )
                )
                await session.commit()

            dispatcher = ReviewsApiResources(
                engine, session_factory
            ).github_installation_delivery_dispatcher(client=client, token_provider=TokenProvider())

            async def read_repository() -> Repository | None:
                async with session_factory() as session:
                    repository: Repository | None = await session.scalar(
                        select(Repository).where(Repository.external_id == external_id)
                    )
                    return repository

            delivery = load_github_webhook_fixture(fixture)
            onboarded = await dispatcher.execute(
                VerifiedGitHubDelivery("delivery-real", event_name, delivery).to_receipt()
            )
            after_onboarding = (onboarded.status, await read_repository(), list(requests))
            removed = await dispatcher.execute(
                VerifiedGitHubDelivery(
                    "delivery-real-removal", event_name, removal_of(event_name, delivery)
                ).to_receipt()
            )
            return (*after_onboarding, removed.status, await read_repository(), list(requests))
        finally:
            await client.aclose()
            await engine.dispose()

    (
        onboarded_status,
        onboarded_row,
        requests_after_onboarding,
        removed_status,
        removed_row,
        requests_after_removal,
    ) = asyncio.run(exercise())

    assert onboarded_status is InstallationDeliveryDispatchStatus.ONBOARDED
    assert onboarded_row is not None
    assert onboarded_row.full_name == full_name
    assert onboarded_row.default_branch == "trunk"
    assert onboarded_row.web_url == f"https://example.test/{full_name}"
    assert onboarded_row.enabled is True
    assert requests_after_onboarding == [
        ("GET", repository_path),
        ("GET", f"{repository_path}/git/trees/trunk"),
        ("POST", f"{repository_path}/labels"),
    ]
    assert removed_status is InstallationDeliveryDispatchStatus.ONBOARDED
    assert removed_row is not None
    assert removed_row.enabled is False
    assert requests_after_removal == requests_after_onboarding


@pytest.mark.integration
def test_empty_repository_is_saved_with_the_backend_rules_through_the_real_tree_adapter(
    migrated_onboarding_database: tuple[str, str],
) -> None:
    """GitHub's 409 for a repository without a commit is an empty tree, not an error.

    The real tree adapter reads ``409 Git Repository is empty.`` as no files, so the
    delivery is onboarded and the saved repository gets the rule set of no recognised
    language, ``backend`` (WEBHOOK_WORKER.md, "Empty repositories").
    """
    database_url, schema = migrated_onboarding_database
    installation_id = uuid4()
    repository_path = "/repos/example-owner/example-repo-two"
    requests: list[tuple[str, str]] = []

    class TokenProvider:
        async def get_installation_access_token(self, installation_external_id: int) -> str:
            assert installation_external_id == 1000001
            return "test-installation-token"

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append((request.method, request.url.path))
        if request.method == "GET" and request.url.path == repository_path:
            return httpx.Response(
                200,
                json={
                    "default_branch": "main",
                    "html_url": "https://example.test/example-owner/example-repo-two",
                },
            )
        if request.method == "GET" and request.url.path == f"{repository_path}/git/trees/main":
            return httpx.Response(409, json={"message": "Git Repository is empty."})
        if request.method == "POST" and request.url.path == f"{repository_path}/labels":
            return httpx.Response(201, json={})
        return httpx.Response(500, json={"message": "unexpected request"})

    async def exercise() -> tuple[
        InstallationDeliveryDispatchStatus, Repository | None, list[RuleVersion]
    ]:
        engine = create_async_engine(
            database_url, connect_args={"options": f"-csearch_path={schema}"}
        )
        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url="https://api.github.com"
        )
        try:
            async with session_factory() as session:
                workspace = Workspace(id=uuid4(), name="empty-repo", daily_budget_usd=Decimal("1"))
                session.add(workspace)
                await session.flush()
                session.add(
                    ProviderInstallation(
                        id=installation_id,
                        workspace_id=workspace.id,
                        provider="github",
                        external_id=1000001,
                        provider_metadata={},
                    )
                )
                await session.commit()

            dispatcher = ReviewsApiResources(
                engine, session_factory
            ).github_installation_delivery_dispatcher(client=client, token_provider=TokenProvider())
            result = await dispatcher.execute(
                VerifiedGitHubDelivery(
                    "delivery-empty-repository",
                    "installation_repositories",
                    load_github_webhook_fixture("installation_repositories_added"),
                ).to_receipt()
            )

            async with session_factory() as session:
                repository = await session.scalar(
                    select(Repository).where(Repository.external_id == 1000005)
                )
                versions = (
                    []
                    if repository is None
                    else list(
                        (
                            await session.scalars(
                                select(RuleVersion).where(
                                    RuleVersion.repository_id == repository.id
                                )
                            )
                        ).all()
                    )
                )
            return result.status, repository, versions
        finally:
            await client.aclose()
            await engine.dispose()

    status, repository, versions = asyncio.run(exercise())

    assert status is InstallationDeliveryDispatchStatus.ONBOARDED
    assert requests == [
        ("GET", repository_path),
        ("GET", f"{repository_path}/git/trees/main"),
        ("POST", f"{repository_path}/labels"),
    ]
    assert repository is not None
    assert repository.full_name == "example-owner/example-repo-two"
    assert repository.enabled is True
    assert [(version.version, version.is_active) for version in versions] == [(1, True)]
    assert (
        versions[0].rules
        == load_default_rule_sets(Path(__file__).resolve().parents[1] / "review" / "rules")[
            "backend"
        ].rules
    )


@pytest.mark.integration
def test_unreadable_repository_details_defer_the_delivery_and_write_no_row(
    migrated_onboarding_database: tuple[str, str],
) -> None:
    """``GET /repos`` answering 404 defers the delivery instead of raising (api#71 AC2).

    The dispatcher must return ``DEFERRED_REPOSITORY_DETAILS`` so the receipt takes the
    deferred path that ``wake_receipts`` can revive, and nothing may reach
    ``repositories``: no tree fetch, no label, no transaction.
    """
    database_url, schema = migrated_onboarding_database
    installation_id = uuid4()
    repository_path = "/repos/example-owner/example-repo-two"
    requests: list[tuple[str, str]] = []

    class TokenProvider:
        async def get_installation_access_token(self, installation_external_id: int) -> str:
            assert installation_external_id == 1000001
            return "test-installation-token"

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append((request.method, request.url.path))
        if request.method == "GET" and request.url.path == repository_path:
            return httpx.Response(404, json={"message": "Not Found"})
        return httpx.Response(500, json={"message": "unexpected request"})

    async def exercise() -> tuple[InstallationDeliveryDispatchStatus, int]:
        engine = create_async_engine(
            database_url, connect_args={"options": f"-csearch_path={schema}"}
        )
        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url="https://api.github.com"
        )
        try:
            async with session_factory() as session:
                workspace = Workspace(id=uuid4(), name="unreadable", daily_budget_usd=Decimal("1"))
                session.add(workspace)
                await session.flush()
                session.add(
                    ProviderInstallation(
                        id=installation_id,
                        workspace_id=workspace.id,
                        provider="github",
                        external_id=1000001,
                        provider_metadata={},
                    )
                )
                await session.commit()

            dispatcher = ReviewsApiResources(
                engine, session_factory
            ).github_installation_delivery_dispatcher(client=client, token_provider=TokenProvider())
            deferred = await dispatcher.execute(
                VerifiedGitHubDelivery(
                    "delivery-unreadable",
                    "installation_repositories",
                    load_github_webhook_fixture("installation_repositories_added"),
                ).to_receipt()
            )

            async with session_factory() as session:
                repositories = list(
                    (
                        await session.scalars(
                            select(Repository).where(
                                Repository.provider_installation_id == installation_id
                            )
                        )
                    ).all()
                )
                rule_versions = list((await session.scalars(select(RuleVersion))).all())
            return deferred.status, len(repositories) + len(rule_versions)
        finally:
            await client.aclose()
            await engine.dispose()

    status, persisted_rows = asyncio.run(exercise())

    assert status is InstallationDeliveryDispatchStatus.DEFERRED_REPOSITORY_DETAILS
    assert persisted_rows == 0
    assert requests == [("GET", repository_path)]


@pytest.mark.integration
def test_unreadable_second_repository_defers_but_the_readable_first_one_is_persisted(
    migrated_onboarding_database: tuple[str, str],
) -> None:
    """One 404 among two repositories defers the delivery without discarding the other row.

    The dispatcher still returns ``DEFERRED_REPOSITORY_DETAILS`` (the receipt is retried and
    revived, and a replay is idempotent), but the readable repository is connected with its
    default rules in the same run.
    """
    database_url, schema = migrated_onboarding_database
    installation_id = uuid4()
    readable_path = "/repos/example-owner/example-repo-two"
    unreadable_path = "/repos/example-owner/example-repo-three"
    requests: list[tuple[str, str]] = []

    class TokenProvider:
        async def get_installation_access_token(self, installation_external_id: int) -> str:
            assert installation_external_id == 1000001
            return "test-installation-token"

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append((request.method, request.url.path))
        if request.method == "GET" and request.url.path == unreadable_path:
            return httpx.Response(404, json={"message": "Not Found"})
        if request.method == "GET" and request.url.path == readable_path:
            return httpx.Response(
                200,
                json={
                    "default_branch": "trunk",
                    "html_url": "https://example.test/example-owner/example-repo-two",
                },
            )
        if request.method == "GET" and request.url.path == f"{readable_path}/git/trees/trunk":
            return httpx.Response(200, json={"tree": [{"path": "src/app.ts", "type": "blob"}]})
        if request.method == "POST" and request.url.path == f"{readable_path}/labels":
            return httpx.Response(201, json={})
        return httpx.Response(500, json={"message": "unexpected request"})

    async def exercise() -> tuple[InstallationDeliveryDispatchStatus, list[Repository]]:
        engine = create_async_engine(
            database_url, connect_args={"options": f"-csearch_path={schema}"}
        )
        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url="https://api.github.com"
        )
        try:
            async with session_factory() as session:
                workspace = Workspace(id=uuid4(), name="partial", daily_budget_usd=Decimal("1"))
                session.add(workspace)
                await session.flush()
                session.add(
                    ProviderInstallation(
                        id=installation_id,
                        workspace_id=workspace.id,
                        provider="github",
                        external_id=1000001,
                        provider_metadata={},
                    )
                )
                await session.commit()

            dispatcher = ReviewsApiResources(
                engine, session_factory
            ).github_installation_delivery_dispatcher(client=client, token_provider=TokenProvider())
            delivery = load_github_webhook_fixture("installation_repositories_added")
            delivery["repositories_added"].append(
                {
                    "id": 1000006,
                    "node_id": "R_kgDOExampleThree",
                    "name": "example-repo-three",
                    "full_name": "example-owner/example-repo-three",
                    "private": False,
                }
            )
            deferred = await dispatcher.execute(
                VerifiedGitHubDelivery(
                    "delivery-partial", "installation_repositories", delivery
                ).to_receipt()
            )

            async with session_factory() as session:
                repositories = list(
                    (
                        await session.scalars(
                            select(Repository).where(
                                Repository.provider_installation_id == installation_id
                            )
                        )
                    ).all()
                )
            return deferred.status, repositories
        finally:
            await client.aclose()
            await engine.dispose()

    status, repositories = asyncio.run(exercise())

    assert status is InstallationDeliveryDispatchStatus.DEFERRED_REPOSITORY_DETAILS
    assert [(row.external_id, row.full_name, row.enabled) for row in repositories] == [
        (1000005, "example-owner/example-repo-two", True)
    ]
    assert sorted(requests) == sorted(
        [
            ("GET", readable_path),
            ("GET", f"{readable_path}/git/trees/trunk"),
            ("POST", f"{readable_path}/labels"),
            ("GET", unreadable_path),
        ]
    )
