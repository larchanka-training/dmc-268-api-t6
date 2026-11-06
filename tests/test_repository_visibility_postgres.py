"""The saved visibility of a repository in a migrated PostgreSQL (#454)."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import cast
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.bootstrap.reviews_api import ReviewsApiResources
from app.modules.integrations.webhooks.api.receipt import VerifiedGitHubDelivery
from app.modules.integrations.webhooks.application.receive_github_delivery import (
    ReceiveGitHubDelivery,
)
from app.modules.repositories.application.installation_repositories import RepositorySnapshot
from app.modules.repositories.application.repository_visibility import RepositoryVisibilityDue
from app.modules.repositories.infrastructure.installation_repository_unit_of_work import (
    SqlAlchemyInstallationRepositoryStore,
)
from app.modules.repositories.infrastructure.repository_visibility import (
    SqlAlchemyRepositoryVisibilityStore,
)
from app.modules.workspaces.application.link_github_installations import (
    AuthenticatedGitHubInstallations,
    GitHubInstallation,
    LinkGitHubInstallations,
)
from app.modules.workspaces.infrastructure.github_installation_links import (
    SqlAlchemyGitHubInstallationLinkUnitOfWork,
)
from tests.portal_postgres import HEAD, INSTALL_A, REPO_A, REPO_B, Env

pytest_plugins = ("tests.portal_postgres",)
pytestmark = pytest.mark.integration

GITHUB_INSTALLATION = 17
GITHUB_REPOSITORY = 101
T0 = datetime(2026, 11, 1, 12, tzinfo=UTC)
BEFORE = T0 - timedelta(hours=1)
AFTER = T0 + timedelta(hours=1)


class _NoTokens:
    async def get_installation_access_token(self, installation_external_id: int) -> str:
        raise AssertionError("recording the visibility makes no GitHub call")


def _exercise(
    env: Env,
    stored: bool | None,
    step: Callable[[AsyncSession], Awaitable[object]],
    *,
    stamp: datetime | None = None,
) -> tuple[bool | None, datetime | None, object]:
    """Store a flag and stamp for repository A, run ``step`` in a transaction, read them back."""

    async def exercise() -> tuple[bool | None, datetime | None, object]:
        engine = env.engine()
        factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with factory.begin() as session:
                await session.execute(
                    text(
                        "UPDATE repositories SET private = :private, "
                        "private_observed_at = :stamp WHERE id = :id"
                    ),
                    {"private": stored, "stamp": stamp, "id": REPO_A},
                )
            async with factory.begin() as session:
                result = await step(session)
            async with factory() as session:
                row = (
                    await session.execute(
                        text(
                            "SELECT private, private_observed_at FROM repositories WHERE id = :id"
                        ),
                        {"id": REPO_A},
                    )
                ).one()
            return row.private, row.private_observed_at, result
        finally:
            await engine.dispose()

    return asyncio.run(exercise())


@pytest.mark.parametrize(
    ("stored", "stamp", "incoming", "incoming_stamp", "expected", "expected_stamp"),
    [
        pytest.param(None, None, False, T0, False, T0, id="never-told"),
        pytest.param(True, BEFORE, False, T0, False, T0, id="a-newer-read-opens"),
        pytest.param(False, BEFORE, True, T0, True, T0, id="a-newer-read-closes"),
        pytest.param(False, BEFORE, None, T0, None, T0, id="a-newer-read-without-a-flag-closes"),
        pytest.param(True, AFTER, False, T0, True, AFTER, id="an-older-read-loses-to-privatized"),
        pytest.param(False, BEFORE, None, None, False, BEFORE, id="nothing-was-read"),
    ],
)
def test_an_installation_upsert_writes_the_flag_only_when_it_knows_more(
    portal_env: Env,
    stored: bool | None,
    stamp: datetime | None,
    incoming: bool | None,
    incoming_stamp: datetime | None,
    expected: bool | None,
    expected_stamp: datetime | None,
) -> None:
    snapshot = RepositorySnapshot(
        external_id=GITHUB_REPOSITORY,
        full_name="octo/a",
        default_branch="main",
        web_url="https://github.test/octo/a",
        private=incoming,
        private_observed_at=incoming_stamp,
    )

    flag, observed, _ = _exercise(
        portal_env,
        stored,
        lambda session: SqlAlchemyInstallationRepositoryStore(session).upsert_repository(
            INSTALL_A, snapshot
        ),
        stamp=stamp,
    )

    assert (flag, observed) == (expected, expected_stamp)


def _store(session: AsyncSession) -> SqlAlchemyRepositoryVisibilityStore:
    return SqlAlchemyRepositoryVisibilityStore(session)


@pytest.mark.parametrize(
    ("stored", "stamp", "incoming", "expected", "expected_stamp"),
    [
        pytest.param(None, None, True, True, T0, id="fills"),
        pytest.param(True, BEFORE, False, False, T0, id="a-newer-change-opens"),
        pytest.param(False, BEFORE, True, True, T0, id="a-newer-change-closes"),
        pytest.param(True, AFTER, False, True, AFTER, id="a-retried-publicized-loses"),
        pytest.param(False, AFTER, True, False, AFTER, id="a-retried-privatized-loses"),
        pytest.param(False, T0, True, True, T0, id="a-tie-closes"),
        pytest.param(True, T0, False, True, T0, id="a-tie-does-not-open"),
    ],
)
def test_a_visibility_change_wins_only_when_it_is_newer(
    portal_env: Env,
    stored: bool | None,
    stamp: datetime | None,
    incoming: bool,
    expected: bool,
    expected_stamp: datetime,
) -> None:
    flag, observed, known = _exercise(
        portal_env,
        stored,
        lambda session: _store(session).overwrite(
            GITHUB_INSTALLATION, GITHUB_REPOSITORY, incoming, T0
        ),
        stamp=stamp,
    )

    assert (flag, observed) == (expected, expected_stamp)
    assert known is True


@pytest.mark.parametrize(
    ("installation", "repository"),
    [
        pytest.param(GITHUB_INSTALLATION, 999, id="another-repository"),
        pytest.param(18, GITHUB_REPOSITORY, id="another-installation"),
    ],
)
def test_a_visibility_change_touches_only_its_own_repository(
    portal_env: Env, installation: int, repository: int
) -> None:
    flag, observed, known = _exercise(
        portal_env,
        None,
        lambda session: _store(session).overwrite(installation, repository, True, T0),
    )

    assert (flag, observed) == (None, None)
    assert known is False


@pytest.mark.parametrize(
    ("stored", "stamp", "incoming", "expected", "expected_stamp"),
    [
        pytest.param(None, None, False, False, T0, id="fills-unknown-as-public"),
        pytest.param(None, None, True, True, T0, id="fills-unknown-as-private"),
        pytest.param(False, BEFORE, True, True, T0, id="closes"),
        pytest.param(True, BEFORE, False, True, BEFORE, id="never-opens"),
        pytest.param(False, BEFORE, False, False, BEFORE, id="no-write-when-unchanged"),
        pytest.param(None, AFTER, False, None, AFTER, id="a-retry-cannot-refill-a-suspension"),
        pytest.param(False, AFTER, True, False, AFTER, id="a-retry-cannot-close-a-newer-read"),
    ],
)
def test_a_lesson_from_another_event_fills_or_closes_only_when_newer(
    portal_env: Env,
    stored: bool | None,
    stamp: datetime | None,
    incoming: bool,
    expected: bool | None,
    expected_stamp: datetime,
) -> None:
    flag, observed, _ = _exercise(
        portal_env,
        stored,
        lambda session: _store(session).learn(GITHUB_INSTALLATION, GITHUB_REPOSITORY, incoming, T0),
        stamp=stamp,
    )

    assert (flag, observed) == (expected, expected_stamp)


@pytest.mark.parametrize(
    ("stored", "stamp", "expected", "expected_stamp"),
    [
        pytest.param(False, BEFORE, None, T0, id="public"),
        pytest.param(True, BEFORE, None, T0, id="private"),
        pytest.param(None, None, None, T0, id="unknown"),
        pytest.param(False, AFTER, False, AFTER, id="a-retried-suspension-loses-to-a-newer-read"),
    ],
)
def test_a_suspension_forgets_the_flag_of_its_installation_only(
    portal_env: Env,
    stored: bool | None,
    stamp: datetime | None,
    expected: bool | None,
    expected_stamp: datetime,
) -> None:
    async def step(session: AsyncSession) -> tuple[bool, object]:
        await session.execute(
            text(f"UPDATE repositories SET private = false WHERE id = '{REPO_B}'")
        )
        known = await _store(session).forget(GITHUB_INSTALLATION, T0)
        return known, await session.scalar(
            text("SELECT private FROM repositories WHERE id = :id"), {"id": REPO_B}
        )

    flag, observed, result = _exercise(portal_env, stored, step, stamp=stamp)
    known, other_flag = cast(tuple[bool, object], result)

    assert (flag, observed) == (expected, expected_stamp)
    assert known is True
    assert other_flag is False


@pytest.mark.parametrize(
    "statement",
    [
        pytest.param(None, id="unknown-installation"),
        pytest.param(
            f"UPDATE provider_installations SET provider = 'gitlab' WHERE id = '{INSTALL_A}'",
            id="another-provider-with-the-same-id",
        ),
    ],
)
def test_a_suspension_of_no_github_repository_changes_nothing(
    portal_env: Env, statement: str | None
) -> None:
    async def step(session: AsyncSession) -> bool:
        if statement is None:
            return await _store(session).forget(999, T0)
        await session.execute(text(statement))
        return await _store(session).forget(GITHUB_INSTALLATION, T0)

    flag, observed, known = _exercise(portal_env, False, step, stamp=BEFORE)

    assert (flag, observed) == (False, BEFORE)
    assert known is False


def _names(env: Env) -> dict[UUID, tuple[str, str]]:
    async def read() -> dict[UUID, tuple[str, str]]:
        engine = env.engine()
        try:
            async with engine.connect() as connection:
                rows = await connection.execute(
                    text("SELECT id, full_name, web_url FROM repositories")
                )
                return {row.id: (row.full_name, row.web_url) for row in rows}
        finally:
            await engine.dispose()

    return asyncio.run(read())


@pytest.mark.parametrize(
    ("web_url", "expected_url"),
    [
        pytest.param("https://github.test/octo/new", "https://github.test/octo/new", id="new-url"),
        pytest.param(None, "https://github.test/octo/a", id="no-url-keeps-the-old-one"),
    ],
)
def test_a_rename_writes_the_name_of_its_own_repository_only(
    portal_env: Env, web_url: str | None, expected_url: str
) -> None:
    before = _names(portal_env)

    _, _, known = _exercise(
        portal_env,
        False,
        lambda session: _store(session).rename(
            GITHUB_INSTALLATION, GITHUB_REPOSITORY, "octo/new", web_url, T0
        ),
        stamp=T0,
    )

    names = _names(portal_env)
    assert known is True
    assert names[REPO_A] == ("octo/new", expected_url)
    assert names[REPO_B] == before[REPO_B]


@pytest.mark.parametrize(
    ("stored_stamp", "expected_name", "expected_stamp"),
    [
        pytest.param(None, "octo/new", T0, id="never-observed"),
        pytest.param(BEFORE, "octo/new", T0, id="a-newer-rename-wins"),
        pytest.param(AFTER, "octo/later", AFTER, id="a-retried-rename-loses"),
        pytest.param(T0, "octo/later", T0, id="a-tie-keeps-the-name"),
    ],
)
def test_a_rename_wins_only_when_it_is_newer(
    portal_env: Env,
    stored_stamp: datetime | None,
    expected_name: str,
    expected_stamp: datetime,
) -> None:
    async def step(session: AsyncSession) -> tuple[bool, object]:
        await session.execute(
            text(
                "UPDATE repositories SET full_name = 'octo/later', "
                "full_name_observed_at = :stamp WHERE id = :id"
            ),
            {"stamp": stored_stamp, "id": REPO_A},
        )
        known = await _store(session).rename(
            GITHUB_INSTALLATION, GITHUB_REPOSITORY, "octo/new", None, T0
        )
        return known, await session.scalar(
            text("SELECT full_name_observed_at FROM repositories WHERE id = :id"), {"id": REPO_A}
        )

    _, _, result = _exercise(portal_env, False, step, stamp=T0)
    known, stamp = cast(tuple[bool, datetime], result)

    assert known is True, "a losing retry still found its repository"
    assert stamp == expected_stamp
    assert _names(portal_env)[REPO_A][0] == expected_name


@pytest.mark.parametrize(
    ("stored_stamp", "incoming_stamp", "expected_name"),
    [
        pytest.param(AFTER, T0, "octo/later", id="an-old-delivery-keeps-a-later-rename"),
        pytest.param(BEFORE, T0, "octo/a", id="a-newer-delivery-writes"),
        pytest.param(AFTER, None, "octo/a", id="no-stamp-writes-as-before"),
    ],
)
def test_an_installation_upsert_writes_the_name_only_when_it_knows_more(
    portal_env: Env,
    stored_stamp: datetime,
    incoming_stamp: datetime | None,
    expected_name: str,
) -> None:
    async def step(session: AsyncSession) -> None:
        await session.execute(
            text(
                "UPDATE repositories SET full_name = 'octo/later', "
                "full_name_observed_at = :stamp WHERE id = :id"
            ),
            {"stamp": stored_stamp, "id": REPO_A},
        )
        await SqlAlchemyInstallationRepositoryStore(session).upsert_repository(
            INSTALL_A,
            RepositorySnapshot(
                external_id=GITHUB_REPOSITORY,
                full_name="octo/a",
                default_branch="main",
                web_url="https://github.test/octo/a",
                private_observed_at=incoming_stamp,
            ),
        )

    _exercise(portal_env, False, step, stamp=T0)

    assert _names(portal_env)[REPO_A][0] == expected_name


def test_a_rename_of_a_repository_not_connected_changes_nothing(portal_env: Env) -> None:
    before = _names(portal_env)

    _, _, known = _exercise(
        portal_env,
        False,
        lambda session: _store(session).rename(GITHUB_INSTALLATION, 999, "octo/new", None, T0),
        stamp=T0,
    )

    assert known is False
    assert _names(portal_env) == before


@pytest.mark.parametrize(
    ("statements", "stamp", "known", "expected", "expected_stamp"),
    [
        pytest.param((), BEFORE, True, None, T0, id="forgets"),
        pytest.param((), AFTER, True, False, AFTER, id="a-retried-transfer-loses"),
        pytest.param(
            (f"UPDATE repositories SET external_id = 777 WHERE id = '{REPO_A}'",),
            BEFORE,
            False,
            False,
            BEFORE,
            id="another-repository",
        ),
        pytest.param(
            (f"UPDATE provider_installations SET provider = 'gitlab' WHERE id = '{INSTALL_A}'",),
            BEFORE,
            False,
            False,
            BEFORE,
            id="another-provider",
        ),
    ],
)
def test_a_transfer_forgets_the_flag_by_the_repository_id_across_installations(
    portal_env: Env,
    statements: tuple[str, ...],
    stamp: datetime,
    known: bool,
    expected: bool | None,
    expected_stamp: datetime,
) -> None:
    async def step(session: AsyncSession) -> bool:
        for statement in statements:
            await session.execute(text(statement))
        return await _store(session).forget_repository(GITHUB_REPOSITORY, T0)

    flag, observed, result = _exercise(portal_env, False, step, stamp=stamp)

    assert (flag, observed) == (expected, expected_stamp)
    assert result is known


@pytest.mark.parametrize(
    ("statements", "expected"),
    [
        pytest.param((), [(17, 101), (18, 102)], id="never-observed-both"),
        pytest.param(
            (f"UPDATE repositories SET private_observed_at = now() WHERE id = '{REPO_A}'",),
            [(18, 102)],
            id="a-fresh-row-waits",
        ),
        pytest.param(
            (
                "UPDATE repositories SET private_observed_at = now() - interval '25 hours' "
                f"WHERE id = '{REPO_A}'",
                "UPDATE repositories SET private_observed_at = now() - interval '30 hours' "
                f"WHERE id = '{REPO_B}'",
            ),
            [(18, 102), (17, 101)],
            id="oldest-first",
        ),
        pytest.param(
            (
                "UPDATE repositories SET private = true, "
                "private_observed_at = now() - interval '25 hours' "
                f"WHERE id = '{REPO_A}'",
                f"UPDATE repositories SET private_observed_at = now() WHERE id = '{REPO_B}'",
            ),
            [(17, 101)],
            id="a-private-row-is-read-too",
        ),
        pytest.param(
            (f"UPDATE repositories SET installation_available = false WHERE id = '{REPO_A}'",),
            [(18, 102)],
            id="access-removed",
        ),
        pytest.param(
            (f"UPDATE provider_installations SET deleted_at = now() WHERE id = '{INSTALL_A}'",),
            [(18, 102)],
            id="installation-deleted",
        ),
        pytest.param(
            (f"UPDATE provider_installations SET provider = 'gitlab' WHERE id = '{INSTALL_A}'",),
            [(18, 102)],
            id="another-provider",
        ),
    ],
)
def test_the_rows_due_for_a_read(
    portal_env: Env, statements: tuple[str, ...], expected: list[tuple[int, int]]
) -> None:
    async def step(session: AsyncSession) -> RepositoryVisibilityDue:
        for statement in statements:
            await session.execute(text(statement))
        return await _store(session).due(older_than=timedelta(hours=24), limit=50)

    async def now() -> datetime:
        engine = portal_env.engine()
        try:
            async with engine.connect() as connection:
                return cast(datetime, await connection.scalar(text("SELECT now()")))
        finally:
            await engine.dispose()

    before = asyncio.run(now())
    flag_stamp: datetime | None = None
    _, _, result = _exercise(portal_env, None, step, stamp=flag_stamp)
    due = cast(RepositoryVisibilityDue, result)

    assert [
        (target.installation_external_id, target.repository_external_id) for target in due.targets
    ] == expected
    assert due.observed_at >= before


def test_a_batch_of_due_rows_is_limited(portal_env: Env) -> None:
    async def step(session: AsyncSession) -> RepositoryVisibilityDue:
        return await _store(session).due(older_than=timedelta(hours=24), limit=1)

    _, _, result = _exercise(portal_env, None, step)

    assert len(cast(RepositoryVisibilityDue, result).targets) == 1


def test_a_retried_delivery_keeps_its_received_at_and_loses_to_a_later_change(
    portal_env: Env,
) -> None:
    """The real receipt path: claimed from the row, stamped with the time it was received."""

    async def exercise() -> tuple[bool | None, datetime | None, datetime | None]:
        engine = portal_env.engine()
        factory = async_sessionmaker(engine, expire_on_commit=False)
        client = httpx.AsyncClient(base_url="https://api.github.com")
        resources = ReviewsApiResources(engine, factory)
        receiver = ReceiveGitHubDelivery(
            uow_factory=resources.github_webhook_receipts,
            dispatcher=resources.github_installation_delivery_dispatcher(
                client=client, token_provider=_NoTokens()
            ),
        )
        repository = {"id": GITHUB_REPOSITORY, "full_name": "octo/a"}

        async def deliver(delivery: str, action: str) -> None:
            await receiver.execute(
                VerifiedGitHubDelivery(
                    delivery,
                    "repository",
                    {
                        "action": action,
                        "installation": {"id": GITHUB_INSTALLATION},
                        "repository": repository,
                    },
                ).to_receipt()
            )
            await receiver.replay_pending()

        async def read() -> tuple[bool | None, datetime | None]:
            async with factory() as session:
                row = (
                    await session.execute(
                        text(
                            "SELECT private, private_observed_at FROM repositories WHERE id = :id"
                        ),
                        {"id": REPO_A},
                    )
                ).one()
            return row.private, row.private_observed_at

        try:
            await deliver("00000000-0000-4000-8000-000000000454", "publicized")
            opened, stamp = await read()
            assert opened is False
            async with factory.begin() as session:
                received = await session.scalar(
                    text("SELECT received_at FROM webhook_events WHERE delivery_id = :id"),
                    {"id": "00000000-0000-4000-8000-000000000454"},
                )
                # A later privatized; then the old delivery runs again, as a retry would.
                await session.execute(
                    text(
                        "UPDATE repositories SET private = true, "
                        "private_observed_at = :later WHERE id = :id"
                    ),
                    {"later": received + timedelta(seconds=1), "id": REPO_A},
                )
                await session.execute(
                    text("UPDATE webhook_events SET projected_at = NULL WHERE delivery_id = :id"),
                    {"id": "00000000-0000-4000-8000-000000000454"},
                )
            await receiver.replay_pending()
            flag, _ = await read()
            return flag, stamp, received
        finally:
            await client.aclose()
            await engine.dispose()

    flag, stamp, received = asyncio.run(exercise())

    assert flag is True
    assert stamp == received


def test_the_runtime_dispatcher_records_visibility_from_both_events(portal_env: Env) -> None:
    async def exercise() -> tuple[bool | None, str, bool | None, str, bool | None, str]:
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
            forgotten, forgotten_status = await deliver(
                "installation", {"action": "suspend", "installation": installation}
            )
        finally:
            await client.aclose()
            await engine.dispose()
        return learned, learned_status, closed, closed_status, forgotten, forgotten_status

    assert asyncio.run(exercise()) == (
        False,
        "ignored_irrelevant_event",
        True,
        "updated_repository_visibility",
        None,
        "updated_repository_visibility",
    )


class _FreshOAuth:
    """The same account (700) as a new installation, after the old one was deleted."""

    async def identify_user(self, access_token: str) -> int:
        return 42

    async def list_for_user(self, access_token: str) -> AuthenticatedGitHubInstallations:
        return AuthenticatedGitHubInstallations(
            42, (GitHubInstallation(29, "octo", (GITHUB_REPOSITORY,), 700),)
        )


def test_a_rebind_forgets_what_was_saved_before_the_app_was_gone(portal_env: Env) -> None:
    async def exercise() -> tuple[bool, bool | None, bool]:
        engine = portal_env.engine()
        factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with factory.begin() as session:
                await session.execute(
                    text(
                        "UPDATE provider_installations SET account_external_id = 700, "
                        "deleted_at = clock_timestamp() - interval '1 day' WHERE id = :id"
                    ),
                    {"id": INSTALL_A},
                )
                await session.execute(
                    text(
                        "UPDATE repositories SET installation_available = false, private = false "
                        "WHERE id = :id"
                    ),
                    {"id": REPO_A},
                )
                await session.execute(
                    text(
                        "DELETE FROM github_user_repository_access "
                        "WHERE provider_installation_id = :id"
                    ),
                    {"id": INSTALL_A},
                )
                await session.execute(
                    text("UPDATE runs SET state = 'succeeded' WHERE state = 'queued'")
                )
            await LinkGitHubInstallations(
                github=_FreshOAuth(),
                uow_factory=lambda: SqlAlchemyGitHubInstallationLinkUnitOfWork(factory),
            ).execute("fresh-user-token")
            async with factory() as session:
                row = (
                    await session.execute(
                        text(
                            "SELECT installation_available, private, "
                            "private_observed_at IS NOT NULL AS stamped "
                            "FROM repositories WHERE id = :id"
                        ),
                        {"id": REPO_A},
                    )
                ).one()
            return row.installation_available, row.private, row.stamped
        finally:
            await engine.dispose()

    assert asyncio.run(exercise()) == (True, None, True)
