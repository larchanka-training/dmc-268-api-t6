"""A deleted GitHub App reinstall stays on the original workspace in /auth/me."""

from __future__ import annotations

import asyncio
import selectors
import sys
from collections.abc import Coroutine

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.bootstrap.portal_auth import get_auth_scope
from app.main import app
from app.modules.auth.application.scope import AuthScope
from app.modules.auth.infrastructure.sessions import SqlAlchemyAuthSessionUnitOfWork
from app.modules.workspaces.application.link_github_installations import (
    AuthenticatedGitHubInstallations,
    GitHubInstallation,
    LinkGitHubInstallations,
)
from app.modules.workspaces.infrastructure.github_installation_links import (
    SqlAlchemyGitHubInstallationLinkUnitOfWork,
)
from tests.portal_postgres import INSTALL_A, WS_A, Env, api

pytest_plugins = ("tests.portal_postgres",)


def _run[T](coro: Coroutine[object, object, T]) -> T:
    return asyncio.run(
        coro,
        loop_factory=lambda: asyncio.SelectorEventLoop(selectors.SelectSelector()),
    )


@pytest.mark.integration
def test_reinstall_with_a_new_installation_id_keeps_one_workspace_in_auth_me(
    portal_env: Env,
) -> None:
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    async def retire_and_reinstall(
        factory: async_sessionmaker[AsyncSession],
    ) -> tuple[tuple[object, ...], int]:
        async with factory.begin() as session:
            await session.execute(
                text(
                    "UPDATE provider_installations SET account_external_id=700, "
                    "deleted_at=clock_timestamp() - interval '1 day' WHERE id=:id"
                ),
                {"id": INSTALL_A},
            )
        workspace_ids = await LinkGitHubInstallations(
            github=_FreshGitHub(),
            uow_factory=lambda: SqlAlchemyGitHubInstallationLinkUnitOfWork(factory),
        ).execute("fresh-user-token")
        assert workspace_ids == (WS_A,)
        async with SqlAlchemyAuthSessionUnitOfWork(factory) as uow:
            _user, identity = await uow.sessions.current_identity(42)
        async with factory() as session:
            workspace_count = await session.scalar(text("SELECT count(*) FROM workspaces"))
        assert workspace_count is not None
        return identity, int(workspace_count)

    with api(portal_env, AuthScope(42, (WS_A,))) as (client, factory):
        identity, workspace_count = _run(retire_and_reinstall(factory))
        assert identity == (WS_A,)
        assert workspace_count == 2
        app.dependency_overrides[get_auth_scope] = lambda: AuthScope(42, identity)
        listed = client.get("/api/auth/me")
        assert listed.status_code == 200
        assert listed.json()["workspaces"] == [
            {"id": str(WS_A), "name": "W", "installationId": 29},
        ]


class _FreshGitHub:
    async def identify_user(self, access_token: str) -> int:
        return 42

    async def list_for_user(self, access_token: str) -> AuthenticatedGitHubInstallations:
        return AuthenticatedGitHubInstallations(
            42, (GitHubInstallation(29, "renamed-owner", (101,), 700),)
        )
