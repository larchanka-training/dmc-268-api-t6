"""The saved visibility of a repository in a migrated PostgreSQL (#454)."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.bootstrap.reviews_api import ReviewsApiResources
from app.modules.integrations.webhooks.api.receipt import VerifiedGitHubDelivery
from app.modules.repositories.application.installation_repositories import RepositorySnapshot
from app.modules.repositories.infrastructure.installation_repository_unit_of_work import (
    SqlAlchemyInstallationRepositoryStore,
)
from app.modules.repositories.infrastructure.repository_visibility import (
    SqlAlchemyRepositoryVisibilityStore,
)
from tests.portal_postgres import HEAD, INSTALL_A, REPO_A, Env

pytest_plugins = ("tests.portal_postgres",)
pytestmark = pytest.mark.integration

GITHUB_INSTALLATION = 17
GITHUB_REPOSITORY = 101


class _NoTokens:
    async def get_installation_access_token(self, installation_external_id: int) -> str:
        raise AssertionError("recording the visibility makes no GitHub call")


def _exercise(
    env: Env, stored: bool | None, step: Callable[[AsyncSession], Awaitable[object]]
) -> tuple[bool | None, object]:
    """Store ``stored`` for repository A, run ``step`` in a transaction, read the flag back."""

    async def exercise() -> tuple[bool | None, object]:
        engine = env.engine()
        factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with factory.begin() as session:
                await session.execute(
                    text("UPDATE repositories SET private = :private WHERE id = :id"),
                    {"private": stored, "id": REPO_A},
                )
            async with factory.begin() as session:
                result = await step(session)
            async with factory() as session:
                flag = await session.scalar(
                    text("SELECT private FROM repositories WHERE id = :id"), {"id": REPO_A}
                )
            return flag, result
        finally:
            await engine.dispose()

    return asyncio.run(exercise())


@pytest.mark.parametrize(
    ("stored", "incoming", "expected"),
    [
        pytest.param(None, False, False, id="unknown-to-public"),
        pytest.param(None, True, True, id="unknown-to-private"),
        pytest.param(True, False, False, id="private-to-public"),
        pytest.param(False, True, True, id="public-to-private"),
        pytest.param(False, None, None, id="unknown-is-not-kept-as-public"),
    ],
)
def test_an_installation_upsert_writes_the_flag_it_knows(
    portal_env: Env, stored: bool | None, incoming: bool | None, expected: bool | None
) -> None:
    snapshot = RepositorySnapshot(
        external_id=GITHUB_REPOSITORY,
        full_name="octo/a",
        default_branch="main",
        web_url="https://github.test/octo/a",
        private=incoming,
    )

    flag, _ = _exercise(
        portal_env,
        stored,
        lambda session: SqlAlchemyInstallationRepositoryStore(session).upsert_repository(
            INSTALL_A, snapshot
        ),
    )

    assert flag is expected


def _store(session: AsyncSession) -> SqlAlchemyRepositoryVisibilityStore:
    return SqlAlchemyRepositoryVisibilityStore(session)


@pytest.mark.parametrize(
    ("stored", "incoming"),
    [(None, True), (None, False), (True, False), (False, True), (True, True)],
)
def test_a_visibility_event_overwrites_whatever_is_saved(
    portal_env: Env, stored: bool | None, incoming: bool
) -> None:
    flag, known = _exercise(
        portal_env,
        stored,
        lambda session: _store(session).overwrite(GITHUB_INSTALLATION, GITHUB_REPOSITORY, incoming),
    )

    assert flag is incoming
    assert known is True


@pytest.mark.parametrize(
    ("installation", "repository"),
    [
        pytest.param(GITHUB_INSTALLATION, 999, id="another-repository"),
        pytest.param(18, GITHUB_REPOSITORY, id="another-installation"),
    ],
)
def test_a_visibility_event_touches_only_its_own_repository(
    portal_env: Env, installation: int, repository: int
) -> None:
    flag, known = _exercise(
        portal_env,
        None,
        lambda session: _store(session).overwrite(installation, repository, True),
    )

    assert flag is None
    assert known is False


@pytest.mark.parametrize(
    ("stored", "incoming", "expected"),
    [
        pytest.param(None, False, False, id="fills-unknown-as-public"),
        pytest.param(None, True, True, id="fills-unknown-as-private"),
        pytest.param(False, True, True, id="closes"),
        pytest.param(True, False, True, id="never-opens"),
        pytest.param(False, False, False, id="public-stays"),
        pytest.param(True, True, True, id="private-stays"),
    ],
)
def test_a_lesson_from_another_event_fills_or_closes_but_never_opens(
    portal_env: Env, stored: bool | None, incoming: bool, expected: bool
) -> None:
    flag, _ = _exercise(
        portal_env,
        stored,
        lambda session: _store(session).learn(GITHUB_INSTALLATION, GITHUB_REPOSITORY, incoming),
    )

    assert flag is expected


def test_the_runtime_dispatcher_records_visibility_from_both_events(portal_env: Env) -> None:
    async def exercise() -> tuple[bool | None, str, bool | None, str]:
        engine = portal_env.engine()
        factory = async_sessionmaker(engine, expire_on_commit=False)
        client = httpx.AsyncClient(base_url="https://api.github.com")
        adapter = ReviewsApiResources(engine, factory).github_installation_delivery_dispatcher(
            client=client, token_provider=_NoTokens()
        )

        async def deliver(event_name: str, payload: dict[str, object]) -> tuple[bool | None, str]:
            result = await adapter.execute(
                VerifiedGitHubDelivery(str(uuid4()), event_name, payload).to_receipt()
            )
            async with factory() as session:
                flag = await session.scalar(
                    text("SELECT private FROM repositories WHERE id = :id"), {"id": REPO_A}
                )
            return flag, result.status.value

        repository = {"id": GITHUB_REPOSITORY, "full_name": "octo/a"}
        installation = {"id": GITHUB_INSTALLATION}
        try:
            learned, learned_status = await deliver(
                "pull_request",
                {
                    "action": "review_requested",
                    "installation": installation,
                    "repository": {**repository, "private": False},
                    "pull_request": {
                        "id": 901,
                        "number": 1,
                        "title": "PR",
                        "html_url": "https://github.test/octo/a/pull/1",
                        "user": {"login": "octocat"},
                        "head": {"ref": "feature", "sha": HEAD},
                        "base": {"ref": "main", "sha": "b" * 40},
                        "state": "open",
                        "updated_at": "2026-11-01T00:00:00Z",
                    },
                },
            )
            closed, closed_status = await deliver(
                "repository",
                {"action": "privatized", "installation": installation, "repository": repository},
            )
        finally:
            await client.aclose()
            await engine.dispose()
        return learned, learned_status, closed, closed_status

    assert asyncio.run(exercise()) == (
        False,
        "ignored_irrelevant_event",
        True,
        "updated_repository_visibility",
    )
