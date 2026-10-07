"""Authenticated installation linking and durable event replay."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from types import TracebackType
from typing import Self, cast
from uuid import UUID, uuid4

import httpx
import pytest
from alembic.config import Config
from sqlalchemy import create_engine, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.schema import CreateSchema, DropSchema

from alembic import command
from app.modules.integrations.webhooks.api.dispatch import GitHubWebhookDispatchAdapter
from app.modules.integrations.webhooks.api.receipt import VerifiedGitHubDelivery
from app.modules.integrations.webhooks.application.github_installation_dispatch import (
    GitHubInstallationDeliveryDispatcher,
)
from app.modules.integrations.webhooks.application.receive_github_delivery import (
    ReceiveGitHubDelivery,
)
from app.modules.integrations.webhooks.infrastructure.github_installation_resolver import (
    SqlAlchemyGitHubInstallationResolver,
)
from app.modules.integrations.webhooks.infrastructure.github_webhook_receipts import (
    SqlAlchemyGitHubWebhookReceiptUnitOfWork,
)
from app.modules.integrations.webhooks.infrastructure.models import WebhookEvent
from app.modules.repositories.application.installation_repositories import (
    InstallationRepositoriesEvent,
)
from app.modules.repositories.application.onboard_repository import OnboardingResult
from app.modules.repositories.infrastructure.models import ProviderInstallation
from app.modules.workspaces.application.link_github_installations import (
    AuthenticatedGitHubInstallations,
    GitHubInstallation,
    InstallationSnapshotReservation,
    LinkGitHubInstallations,
)
from app.modules.workspaces.infrastructure.github_installation_links import (
    SqlAlchemyGitHubInstallationLinkUnitOfWork,
)
from app.modules.workspaces.infrastructure.github_user_installations import (
    HttpGitHubUserInstallationsProvider,
)
from app.modules.workspaces.infrastructure.models import (
    GitHubUserInstallationSync,
    GitHubUserRepositoryAccess,
    GitHubUserWorkspaceAccess,
    Workspace,
)


@dataclass
class FakeGitHub:
    user: AuthenticatedGitHubInstallations
    calls: list[str] = field(default_factory=list)

    async def identify_user(self, access_token: str) -> int:
        return self.user.user_id

    async def list_for_user(self, access_token: str) -> AuthenticatedGitHubInstallations:
        self.calls.append(access_token)
        return self.user


@dataclass
class FakeLinks:
    installations: dict[int, UUID] = field(default_factory=dict)
    access: set[tuple[int, UUID]] = field(default_factory=set)
    repository_access: set[tuple[int, int, int]] = field(default_factory=set)
    replayable: set[int] = field(default_factory=set)
    link_order: list[int] = field(default_factory=list)
    commits: int = 0
    generations: dict[int, int] = field(default_factory=dict)
    applied: dict[int, int] = field(default_factory=dict)

    @property
    def links(self) -> FakeLinks:
        return self

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        pass

    async def commit(self) -> None:
        self.commits += 1

    async def rollback(self) -> None:
        pass

    async def reserve_generation(self, user_id: int) -> InstallationSnapshotReservation:
        generation = self.generations.get(user_id, 0) + 1
        self.generations[user_id] = generation
        return InstallationSnapshotReservation(generation, datetime.now(UTC))

    async def begin_apply(self, user_id: int, generation: int) -> bool:
        return generation > self.applied.get(user_id, 0)

    async def mark_applied(self, user_id: int, generation: int) -> None:
        self.applied[user_id] = generation

    async def current_workspace_ids(self, user_id: int) -> tuple[UUID, ...]:
        return tuple(sorted(workspace for owner, workspace in self.access if owner == user_id))

    async def link(self, user_id: int, installation: GitHubInstallation) -> UUID:
        self.link_order.append(installation.id)
        workspace_id = self.installations.setdefault(installation.id, uuid4())
        self.access.add((user_id, workspace_id))
        return workspace_id

    async def reconcile_repositories(
        self,
        user_id: int,
        installation_id: int,
        repository_ids: tuple[int, ...],
        snapshot_started_at: datetime,
    ) -> None:
        self.repository_access = {
            entry
            for entry in self.repository_access
            if entry[0] != user_id or entry[1] != installation_id
        }
        self.repository_access.update((user_id, installation_id, item) for item in repository_ids)

    async def wake_receipts(self, installation_id: int) -> None:
        self.replayable.add(installation_id)

    async def revoke_unlisted(
        self, user_id: int, workspace_ids: tuple[UUID, ...], installation_ids: tuple[int, ...]
    ) -> None:
        self.access = {
            entry for entry in self.access if entry[0] != user_id or entry[1] in workspace_ids
        }
        self.repository_access = {
            entry
            for entry in self.repository_access
            if entry[0] != user_id or entry[1] in installation_ids
        }


def test_authenticated_installation_is_linked_once_on_repeat_callback() -> None:
    github = FakeGitHub(AuthenticatedGitHubInstallations(41, (GitHubInstallation(17, "octo"),)))
    links = FakeLinks()
    use_case = LinkGitHubInstallations(github=github, uow_factory=lambda: links)

    first = asyncio.run(use_case.execute("user-token"))
    second = asyncio.run(use_case.execute("user-token"))

    assert first == second
    assert len(first) == 1
    assert links.access == {(41, first[0])}
    assert links.replayable == {17}
    assert github.calls == ["user-token", "user-token"]


def test_zero_installations_is_valid_and_does_not_create_workspace() -> None:
    github = FakeGitHub(AuthenticatedGitHubInstallations(41, ()))
    links = FakeLinks()

    result = asyncio.run(
        LinkGitHubInstallations(github=github, uow_factory=lambda: links).execute("t")
    )

    assert result == ()
    assert links.installations == {}
    assert links.commits == 2


def test_callback_profile_identity_must_match_installation_token_identity() -> None:
    github = FakeGitHub(AuthenticatedGitHubInstallations(41, ()))
    links = FakeLinks()

    with pytest.raises(ValueError, match="identity changed"):
        asyncio.run(
            LinkGitHubInstallations(github=github, uow_factory=lambda: links).execute(
                "user-token", expected_user_id=42
            )
        )

    assert links.commits == 0
    assert links.access == set()


def test_zero_installations_revokes_stale_workspace_access() -> None:
    github = FakeGitHub(
        AuthenticatedGitHubInstallations(41, (GitHubInstallation(17, "octo", (101,)),))
    )
    links = FakeLinks()
    use_case = LinkGitHubInstallations(github=github, uow_factory=lambda: links)
    workspace_ids = asyncio.run(use_case.execute("user-token"))
    github.user = AuthenticatedGitHubInstallations(41, ())

    assert asyncio.run(use_case.execute("user-token")) == ()
    assert workspace_ids == (links.installations[17],)
    assert links.access == set()
    assert links.repository_access == set()


def test_two_users_reconcile_disjoint_repository_grants_without_cross_access() -> None:
    github = FakeGitHub(
        AuthenticatedGitHubInstallations(41, (GitHubInstallation(17, "octo", (101, 102)),))
    )
    links = FakeLinks()
    use_case = LinkGitHubInstallations(github=github, uow_factory=lambda: links)

    first_workspace = asyncio.run(use_case.execute("first-token"))
    github.user = AuthenticatedGitHubInstallations(99, (GitHubInstallation(17, "octo", (103,)),))
    second_workspace = asyncio.run(use_case.execute("second-token"))

    assert first_workspace == second_workspace
    assert links.repository_access == {(41, 17, 101), (41, 17, 102), (99, 17, 103)}

    github.user = AuthenticatedGitHubInstallations(41, (GitHubInstallation(17, "octo", (101,)),))
    asyncio.run(use_case.execute("first-token"))
    assert links.repository_access == {(41, 17, 101), (99, 17, 103)}

    github.user = AuthenticatedGitHubInstallations(41, (GitHubInstallation(17, "octo", ()),))
    assert asyncio.run(use_case.execute("first-token")) == first_workspace
    assert links.repository_access == {(99, 17, 103)}


def test_installations_are_linked_in_stable_id_order() -> None:
    github = FakeGitHub(
        AuthenticatedGitHubInstallations(
            41, (GitHubInstallation(29, "two"), GitHubInstallation(17, "one"))
        )
    )
    links = FakeLinks()

    asyncio.run(LinkGitHubInstallations(github=github, uow_factory=lambda: links).execute("t"))

    assert links.link_order == [17, 29]


def test_older_callback_cannot_restore_grants_after_newer_empty_snapshot() -> None:
    async def exercise() -> tuple[tuple[UUID, ...], tuple[UUID, ...], FakeLinks]:
        older_listing_started = asyncio.Event()
        release_older_listing = asyncio.Event()
        links = FakeLinks()

        class ControlledGitHub:
            async def identify_user(self, access_token: str) -> int:
                return 41

            async def list_for_user(self, access_token: str) -> AuthenticatedGitHubInstallations:
                if access_token == "older":
                    assert links.generations[41] == 1
                    older_listing_started.set()
                    await release_older_listing.wait()
                    return AuthenticatedGitHubInstallations(
                        41, (GitHubInstallation(17, "octo", (101,)),)
                    )
                assert links.generations[41] == 2
                return AuthenticatedGitHubInstallations(41, ())

        use_case = LinkGitHubInstallations(github=ControlledGitHub(), uow_factory=lambda: links)
        older_task = asyncio.create_task(use_case.execute("older"))
        await asyncio.wait_for(older_listing_started.wait(), timeout=2)
        newer_result = await use_case.execute("newer")
        release_older_listing.set()
        older_result = await older_task
        return older_result, newer_result, links

    older_result, newer_result, links = asyncio.run(exercise())

    assert newer_result == ()
    assert older_result == ()
    assert links.installations == {}
    assert links.repository_access == set()
    assert links.applied == {41: 2}


def test_older_callback_can_apply_when_newer_listing_fails() -> None:
    async def exercise() -> tuple[tuple[UUID, ...], FakeLinks]:
        older_listing_started = asyncio.Event()
        release_older_listing = asyncio.Event()
        links = FakeLinks()

        class ControlledGitHub:
            async def identify_user(self, access_token: str) -> int:
                return 41

            async def list_for_user(self, access_token: str) -> AuthenticatedGitHubInstallations:
                if access_token == "older":
                    older_listing_started.set()
                    await release_older_listing.wait()
                    return AuthenticatedGitHubInstallations(
                        41, (GitHubInstallation(17, "octo", (101,)),)
                    )
                assert links.generations[41] == 2
                raise RuntimeError("GitHub listing failed")

        use_case = LinkGitHubInstallations(github=ControlledGitHub(), uow_factory=lambda: links)
        older_task = asyncio.create_task(use_case.execute("older"))
        await asyncio.wait_for(older_listing_started.wait(), timeout=2)
        with pytest.raises(RuntimeError, match="GitHub listing failed"):
            await use_case.execute("newer")
        release_older_listing.set()
        return await older_task, links

    older_result, links = asyncio.run(exercise())

    assert older_result == (links.installations[17],)
    assert links.repository_access == {(41, 17, 101)}
    assert links.applied == {41: 1}


def test_webhook_replay_cannot_grant_access_to_another_user() -> None:
    github = FakeGitHub(AuthenticatedGitHubInstallations(41, (GitHubInstallation(17, "octo"),)))
    links = FakeLinks()
    use_case = LinkGitHubInstallations(github=github, uow_factory=lambda: links)

    workspace_ids = asyncio.run(use_case.execute("owner-token"))
    github.user = AuthenticatedGitHubInstallations(99, ())
    other_workspace_ids = asyncio.run(use_case.execute("other-token"))

    assert other_workspace_ids == ()
    assert links.access == {(41, workspace_ids[0])}
    assert links.replayable == {17}


def test_github_adapter_derives_user_and_paginates_installations_from_same_token() -> None:
    requests: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append((str(request.url), request.headers["Authorization"]))
        if request.url.path == "/user":
            return httpx.Response(200, json={"id": 41})
        if request.url.path.endswith("/repositories"):
            return httpx.Response(200, json={"total_count": 0, "repositories": []})
        if request.url.params["page"] == "1":
            return httpx.Response(
                200,
                json={
                    "total_count": 101,
                    "installations": [
                        {"id": item, "account": {"login": "octo"}} for item in range(1, 101)
                    ],
                },
            )
        return httpx.Response(
            200,
            json={"total_count": 101, "installations": [{"id": 101, "account": {"login": "octo"}}]},
        )

    async def exercise() -> AuthenticatedGitHubInstallations:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url="https://api.github.com"
        ) as client:
            return await HttpGitHubUserInstallationsProvider(client).list_for_user("secret-token")

    result = asyncio.run(exercise())

    assert result.user_id == 41
    assert len(result.installations) == 101
    assert result.installations[0] == GitHubInstallation(1, "octo")
    assert result.installations[-1] == GitHubInstallation(101, "octo")
    assert len(requests) == 104
    assert all(authorization == "Bearer secret-token" for _, authorization in requests)
    assert sum("/repositories" in url for url, _ in requests) == 101


def test_github_adapter_paginates_each_installations_visible_repositories() -> None:
    seen_pages: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/user":
            return httpx.Response(200, json={"id": 41})
        if request.url.path == "/user/installations":
            return httpx.Response(
                200,
                json={
                    "total_count": 1,
                    "installations": [{"id": 17, "account": {"login": "octo"}}],
                },
            )
        assert request.url.path == "/user/installations/17/repositories"
        page = int(request.url.params["page"])
        seen_pages.append(page)
        if page == 1:
            return httpx.Response(
                200,
                json={"total_count": 101, "repositories": [{"id": item} for item in range(1, 101)]},
            )
        return httpx.Response(200, json={"total_count": 101, "repositories": [{"id": 101}]})

    async def exercise() -> AuthenticatedGitHubInstallations:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url="https://api.github.com"
        ) as client:
            return await HttpGitHubUserInstallationsProvider(client).list_for_user("token")

    assert asyncio.run(exercise()) == AuthenticatedGitHubInstallations(
        41, (GitHubInstallation(17, "octo", tuple(range(1, 102))),)
    )
    assert seen_pages == [1, 2]


def test_github_adapter_accepts_zero_installations() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/user":
            return httpx.Response(200, json={"id": 41})
        return httpx.Response(200, json={"total_count": 0, "installations": []})

    async def exercise() -> AuthenticatedGitHubInstallations:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url="https://api.github.com"
        ) as client:
            return await HttpGitHubUserInstallationsProvider(client).list_for_user("token")

    assert asyncio.run(exercise()) == AuthenticatedGitHubInstallations(41, ())


@pytest.fixture
def migrated_link_database() -> Iterator[tuple[str, str]]:
    database_url = os.environ.get("TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("set TEST_DATABASE_URL to run PostgreSQL integration tests")
    schema = f"test_installation_link_{uuid4().hex}"
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


@pytest.mark.integration
def test_concurrent_authenticated_links_share_workspace_and_replay_prior_receipt(
    migrated_link_database: tuple[str, str],
) -> None:
    database_url, schema = migrated_link_database

    @dataclass
    class Onboarding:
        calls: list[tuple[UUID, InstallationRepositoriesEvent]] = field(default_factory=list)

        async def execute(
            self, *, provider_installation_id: UUID, event: InstallationRepositoriesEvent
        ) -> tuple[OnboardingResult, ...]:
            self.calls.append((provider_installation_id, event))
            return ()

    async def exercise() -> None:
        engine = create_async_engine(
            database_url, connect_args={"options": f"-csearch_path={schema}"}
        )
        session_factory = async_sessionmaker(engine, expire_on_commit=False)

        def receipt_factory() -> SqlAlchemyGitHubWebhookReceiptUnitOfWork:
            return SqlAlchemyGitHubWebhookReceiptUnitOfWork(session_factory)

        try:
            delivery = VerifiedGitHubDelivery(
                "before-link",
                "installation_repositories",
                {"action": "added", "installation": {"id": 17}, "repositories_added": []},
            )
            receiver = ReceiveGitHubDelivery(
                uow_factory=receipt_factory,
                dispatcher=GitHubWebhookDispatchAdapter(
                    GitHubInstallationDeliveryDispatcher(
                        resolver=SqlAlchemyGitHubInstallationResolver(session_factory),
                        onboarding=Onboarding(),
                    )
                ),
            )
            await receiver.execute(delivery.to_receipt())
            await receiver.replay_pending()
            async with session_factory() as session:
                receipt = await session.scalar(
                    select(WebhookEvent).where(WebhookEvent.delivery_id == "before-link")
                )
                assert receipt is not None
                assert receipt.retry_after is not None

            first_github = FakeGitHub(
                AuthenticatedGitHubInstallations(41, (GitHubInstallation(17, "octo", (101,)),))
            )
            first = LinkGitHubInstallations(
                github=first_github,
                uow_factory=lambda: SqlAlchemyGitHubInstallationLinkUnitOfWork(session_factory),
            )
            second = LinkGitHubInstallations(
                github=FakeGitHub(
                    AuthenticatedGitHubInstallations(99, (GitHubInstallation(17, "octo", (102,)),))
                ),
                uow_factory=lambda: SqlAlchemyGitHubInstallationLinkUnitOfWork(session_factory),
            )
            first_ids, second_ids = await asyncio.gather(
                first.execute("first-token"), second.execute("second-token")
            )
            assert first_ids == second_ids
            assert len(first_ids) == 1
            assert await first.execute("first-token") == first_ids

            async with session_factory() as session:
                assert len((await session.scalars(select(Workspace))).all()) == 1
                assert len((await session.scalars(select(ProviderInstallation))).all()) == 1
                access = (await session.scalars(select(GitHubUserWorkspaceAccess))).all()
                assert {item.github_user_id for item in access} == {41, 99}
                grants = (await session.scalars(select(GitHubUserRepositoryAccess))).all()
                assert {(item.github_user_id, item.repository_external_id) for item in grants} == {
                    (41, 101),
                    (99, 102),
                }
                receipt = await session.scalar(
                    select(WebhookEvent).where(WebhookEvent.delivery_id == "before-link")
                )
                assert receipt is not None
                assert receipt.retry_after is None

            first_github.user = AuthenticatedGitHubInstallations(
                41, (GitHubInstallation(17, "octo", ()),)
            )
            assert await first.execute("first-token") == first_ids
            async with session_factory() as session:
                grants = (await session.scalars(select(GitHubUserRepositoryAccess))).all()
                assert {(item.github_user_id, item.repository_external_id) for item in grants} == {
                    (99, 102)
                }

            onboarding = Onboarding()
            replay = ReceiveGitHubDelivery(
                uow_factory=receipt_factory,
                dispatcher=GitHubWebhookDispatchAdapter(
                    GitHubInstallationDeliveryDispatcher(
                        resolver=SqlAlchemyGitHubInstallationResolver(session_factory),
                        onboarding=onboarding,
                    )
                ),
            )
            assert await replay.replay_pending() == 1
            assert len(onboarding.calls) == 1
            assert onboarding.calls[0][0] == (await _installation_id(session_factory, 17))
            async with session_factory() as session:
                projected = await session.scalar(
                    select(WebhookEvent).where(WebhookEvent.delivery_id == "before-link")
                )
                assert projected is not None
                assert projected.projected_at is not None
        finally:
            await engine.dispose()

    asyncio.run(exercise())


@pytest.mark.integration
def test_concurrent_reversed_multi_installation_callbacks_do_not_deadlock(
    migrated_link_database: tuple[str, str],
) -> None:
    database_url, schema = migrated_link_database

    async def exercise() -> None:
        engine = create_async_engine(
            database_url, connect_args={"options": f"-csearch_path={schema}"}
        )
        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            first = LinkGitHubInstallations(
                github=FakeGitHub(
                    AuthenticatedGitHubInstallations(
                        41, (GitHubInstallation(17, "one"), GitHubInstallation(29, "two"))
                    )
                ),
                uow_factory=lambda: SqlAlchemyGitHubInstallationLinkUnitOfWork(session_factory),
            )
            second = LinkGitHubInstallations(
                github=FakeGitHub(
                    AuthenticatedGitHubInstallations(
                        99, (GitHubInstallation(29, "two"), GitHubInstallation(17, "one"))
                    )
                ),
                uow_factory=lambda: SqlAlchemyGitHubInstallationLinkUnitOfWork(session_factory),
            )
            first_ids, second_ids = await asyncio.wait_for(
                asyncio.gather(first.execute("first-token"), second.execute("second-token")),
                timeout=10,
            )
            assert first_ids == second_ids
            assert len(first_ids) == 2
            async with session_factory() as session:
                assert len((await session.scalars(select(Workspace))).all()) == 2
                assert len((await session.scalars(select(ProviderInstallation))).all()) == 2
                assert len((await session.scalars(select(GitHubUserWorkspaceAccess))).all()) == 4
        finally:
            await engine.dispose()

    asyncio.run(exercise())


@pytest.mark.integration
def test_database_newer_snapshot_prevents_older_callback_from_restoring_access(
    migrated_link_database: tuple[str, str],
) -> None:
    database_url, schema = migrated_link_database

    async def exercise() -> None:
        engine = create_async_engine(
            database_url, connect_args={"options": f"-csearch_path={schema}"}
        )
        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        older_started = asyncio.Event()
        release_older = asyncio.Event()

        class ControlledGitHub:
            async def identify_user(self, access_token: str) -> int:
                return 41

            async def list_for_user(self, access_token: str) -> AuthenticatedGitHubInstallations:
                if access_token == "older":
                    older_started.set()
                    await release_older.wait()
                    return AuthenticatedGitHubInstallations(
                        41, (GitHubInstallation(17, "octo", (101,)),)
                    )
                return AuthenticatedGitHubInstallations(41, ())

        use_case = LinkGitHubInstallations(
            github=ControlledGitHub(),
            uow_factory=lambda: SqlAlchemyGitHubInstallationLinkUnitOfWork(session_factory),
        )
        try:
            older_task = asyncio.create_task(use_case.execute("older"))
            await asyncio.wait_for(older_started.wait(), timeout=10)
            assert await use_case.execute("newer") == ()
            release_older.set()
            assert await older_task == ()
            async with session_factory() as session:
                sync = await session.scalar(
                    select(GitHubUserInstallationSync).where(
                        GitHubUserInstallationSync.github_user_id == 41
                    )
                )
                assert sync is not None
                assert sync.reserved_generation == 2
                assert sync.applied_generation == 2
                assert (await session.scalars(select(Workspace))).all() == []
                assert (await session.scalars(select(GitHubUserRepositoryAccess))).all() == []
        finally:
            await engine.dispose()

    asyncio.run(exercise())


async def _installation_id(
    session_factory: async_sessionmaker[AsyncSession], external_id: int
) -> UUID | None:
    async with session_factory() as session:
        return cast(
            UUID | None,
            await session.scalar(
                select(ProviderInstallation.id).where(
                    ProviderInstallation.external_id == external_id
                )
            ),
        )


def test_repeat_login_adds_installation_and_repository_without_losing_existing_grants() -> None:
    github = FakeGitHub(
        AuthenticatedGitHubInstallations(41, (GitHubInstallation(17, "alpha", (101,)),))
    )
    links = FakeLinks()
    use_case = LinkGitHubInstallations(github=github, uow_factory=lambda: links)
    first = asyncio.run(use_case.execute("first-login"))

    github.user = AuthenticatedGitHubInstallations(
        41,
        (
            GitHubInstallation(17, "alpha", (101, 102)),
            GitHubInstallation(18, "beta", (201,)),
        ),
    )
    second = asyncio.run(use_case.execute("sync-access-login"))

    assert len(first) == 1
    assert len(second) == 2
    assert first[0] in second
    assert links.repository_access == {(41, 17, 101), (41, 17, 102), (41, 18, 201)}
