"""Rules playground simulations in PostgreSQL (#311 Part 1).

Opt-in: ``TEST_DATABASE_URL``. The use cases run on the real store and unit of work.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from functools import partial
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.modules.auth.application.scope import AuthScope
from app.modules.reviews.application.rules_simulation import (
    ActiveRulesSimulationExists,
    CreateRulesSimulation,
    GetRulesSimulation,
    RulesSimulationMode,
    RulesSimulationRequest,
    RulesSimulationStatus,
)
from app.modules.reviews.infrastructure.rules_simulation_store import (
    RulesSimulationStoreError,
    SqlAlchemyRulesSimulationStore,
    SqlAlchemyRulesSimulationUnitOfWork,
)
from tests.portal_postgres import REPO_A, REPO_B, WS_A, WS_B, Env

pytest_plugins = ("tests.portal_postgres",)

pytestmark = pytest.mark.integration

_ALICE = AuthScope(42, (WS_A,))
_BOB = AuthScope(43, (WS_A,))
_T0 = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)
_SECRET = "sk-sandbox-SECRET-4f1c"


def _request(repository_id: UUID = REPO_A, content: str = "print(1)\n") -> RulesSimulationRequest:
    return RulesSimulationRequest(
        repository_id=repository_id,
        mode=RulesSimulationMode.CODE,
        path="src/app.py",
        content=content,
        rules_md="```review-ignore\ngenerated/**\n```\n",
    )


def _run(env: Env, scenario: Callable[[async_sessionmaker[Any]], Awaitable[None]]) -> None:
    async def main() -> None:
        engine = env.engine()
        try:
            await scenario(async_sessionmaker(engine, expire_on_commit=False))
        finally:
            await engine.dispose()

    asyncio.run(main())


def _create(
    factory: async_sessionmaker[Any], scope: AuthScope, at: datetime
) -> CreateRulesSimulation:
    return CreateRulesSimulation(
        partial(SqlAlchemyRulesSimulationUnitOfWork, factory, scope), now=lambda: at
    )


def _get(factory: async_sessionmaker[Any], scope: AuthScope, at: datetime) -> GetRulesSimulation:
    return GetRulesSimulation(
        partial(SqlAlchemyRulesSimulationUnitOfWork, factory, scope), now=lambda: at
    )


async def _rows(factory: async_sessionmaker[Any]) -> list[Any]:
    async with factory() as session:
        result = await session.execute(
            text(
                "SELECT id, repository_id, github_user_id, mode, path, content, rules_md, "
                "status, error_code, created_at, finished_at FROM rules_simulations "
                "ORDER BY created_at, id"
            )
        )
        return list(result)


async def _add_bob(factory: async_sessionmaker[Any]) -> None:
    """User 43 sees repository A, as user 42 does."""
    async with factory() as session:
        await session.execute(
            text("INSERT INTO github_user_profiles (id, login, name) VALUES (43, 'bob', 'bob')")
        )
        await session.execute(
            text(
                "INSERT INTO github_user_workspace_access (github_user_id, workspace_id) "
                "VALUES (43, :ws)"
            ),
            {"ws": WS_A},
        )
        await session.execute(
            text(
                "INSERT INTO github_user_repository_access (github_user_id, "
                "provider_installation_id, repository_external_id) "
                "SELECT 43, provider_installation_id, external_id FROM repositories WHERE id = :id"
            ),
            {"id": REPO_A},
        )
        await session.commit()


def test_a_simulation_is_stored_queued_with_its_input_and_read_back(portal_env: Env) -> None:
    async def scenario(factory: async_sessionmaker[Any]) -> None:
        created = await _create(factory, _ALICE, _T0).execute(_request())
        assert created is not None
        read = await _get(factory, _ALICE, _T0 + timedelta(minutes=1)).execute(created.id)
        [row] = await _rows(factory)

        assert read == created
        assert (created.status, created.created_at, created.finished_at) == (
            RulesSimulationStatus.QUEUED,
            _T0,
            None,
        )
        assert tuple(row) == (
            created.id,
            REPO_A,
            42,
            "code",
            "src/app.py",
            "print(1)\n",
            "```review-ignore\ngenerated/**\n```\n",
            "queued",
            None,
            _T0,
            None,
        )

    _run(portal_env, scenario)


@pytest.mark.parametrize("repository_id", [REPO_B, UUID(int=5)], ids=["other-workspace", "unknown"])
def test_a_repository_the_caller_cannot_see_stores_nothing(
    portal_env: Env, repository_id: UUID
) -> None:
    async def scenario(factory: async_sessionmaker[Any]) -> None:
        created = await _create(factory, _ALICE, _T0).execute(_request(repository_id))

        assert created is None
        assert await _rows(factory) == []

    _run(portal_env, scenario)


def test_one_active_simulation_per_user_and_the_refusal_names_it(portal_env: Env) -> None:
    async def scenario(factory: async_sessionmaker[Any]) -> None:
        await _add_bob(factory)
        first = await _create(factory, _ALICE, _T0).execute(_request())
        assert first is not None

        with pytest.raises(ActiveRulesSimulationExists) as caught:
            await _create(factory, _ALICE, _T0 + timedelta(seconds=599)).execute(_request())
        bobs = await _create(factory, _BOB, _T0).execute(_request())

        assert caught.value.simulation_id == first.id
        assert bobs is not None
        assert len(await _rows(factory)) == 2

    _run(portal_env, scenario)


def test_two_simultaneous_creates_store_one_and_refuse_the_other(portal_env: Env) -> None:
    async def scenario(factory: async_sessionmaker[Any]) -> None:
        results = await asyncio.gather(
            _create(factory, _ALICE, _T0).execute(_request()),
            _create(factory, _ALICE, _T0).execute(_request()),
            return_exceptions=True,
        )
        [row] = await _rows(factory)

        refused = [item for item in results if isinstance(item, ActiveRulesSimulationExists)]
        created = [item for item in results if not isinstance(item, BaseException)]
        assert len(refused) == len(created) == 1
        assert refused[0].simulation_id == row.id

    _run(portal_env, scenario)


def test_an_overdue_simulation_reads_as_failed_and_frees_the_user(portal_env: Env) -> None:
    async def scenario(factory: async_sessionmaker[Any]) -> None:
        first = await _create(factory, _ALICE, _T0).execute(_request())
        assert first is not None
        deadline = _T0 + timedelta(minutes=10)

        before = await _get(factory, _ALICE, deadline - timedelta(seconds=1)).execute(first.id)
        at = await _get(factory, _ALICE, deadline).execute(first.id)
        second = await _create(factory, _ALICE, deadline).execute(_request())

        assert before is not None and before.status is RulesSimulationStatus.QUEUED
        assert at is not None
        assert (at.status, at.error_code, at.finished_at) == (
            RulesSimulationStatus.FAILED,
            "deadline_exceeded",
            deadline,
        )
        assert second is not None and second.status is RulesSimulationStatus.QUEUED

    _run(portal_env, scenario)


def test_a_create_after_the_deadline_fails_the_overdue_one_itself(portal_env: Env) -> None:
    async def scenario(factory: async_sessionmaker[Any]) -> None:
        first = await _create(factory, _ALICE, _T0).execute(_request())
        assert first is not None

        second = await _create(factory, _ALICE, _T0 + timedelta(minutes=10)).execute(_request())
        statuses = [(row.id, row.status, row.error_code) for row in await _rows(factory)]

        assert second is not None
        assert statuses == [
            (first.id, "failed", "deadline_exceeded"),
            (second.id, "queued", None),
        ]

    _run(portal_env, scenario)


@pytest.mark.parametrize(
    "reader", [_BOB, AuthScope(42, (WS_B,))], ids=["other-user", "repository-not-visible"]
)
def test_a_simulation_is_read_only_by_its_user_while_the_repository_is_visible(
    portal_env: Env, reader: AuthScope
) -> None:
    async def scenario(factory: async_sessionmaker[Any]) -> None:
        await _add_bob(factory)
        first = await _create(factory, _ALICE, _T0).execute(_request())
        assert first is not None

        assert await _get(factory, reader, _T0).execute(first.id) is None

    _run(portal_env, scenario)


def test_a_failed_insert_does_not_carry_the_pasted_input(portal_env: Env) -> None:
    """A database error names parameters; the pasted code must not reach a log through it."""

    async def scenario(factory: async_sessionmaker[Any]) -> None:
        async with factory() as session:
            store = SqlAlchemyRulesSimulationStore(session, _ALICE)
            # Over the CHECK limit: only a store reached without the use case's checks.
            request = _request(content=_SECRET + "x" * 65_536)
            with pytest.raises(RulesSimulationStoreError) as caught:
                await store.create(UUID(int=9), request, created_at=_T0)

        error = caught.value
        assert _SECRET not in str(error)
        assert error.__cause__ is None
        assert error.__suppress_context__

    _run(portal_env, scenario)
