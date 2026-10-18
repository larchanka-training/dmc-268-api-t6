"""Rules status over REST and its durable read requests in PostgreSQL (#150).

Opt-in: ``TEST_DATABASE_URL``. The worker side runs the real stores; GitHub is either
the mocked API of ``tests/test_rules_status.py`` or a fixed reading.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

import psycopg
import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.modules.auth.application.scope import AuthScope
from app.modules.repositories.application.installation_repositories import RepositorySnapshot
from app.modules.repositories.application.onboard_repository import load_default_rule_sets
from app.modules.repositories.application.rules_status import (
    RefreshRulesStatuses,
    RequestRulesRefreshOnPush,
    RulesDiagnostic,
    RulesReading,
    RulesRefreshTarget,
    RulesStatusKind,
)
from app.modules.repositories.application.sync_installation_repositories import (
    RepositoryOnboardingInput,
    SyncInstallationRepositories,
)
from app.modules.repositories.infrastructure.installation_repository_unit_of_work import (
    SqlAlchemyInstallationRepositoriesUnitOfWork,
)
from app.modules.repositories.infrastructure.rules_status import (
    SqlAlchemyRulesRefreshUnitOfWork,
    request_rules_refresh,
)
from tests.portal_postgres import INSTALL_A, REPO_A, REPO_B, WS_A, Env, api, portal_schema, scalar
from tests.test_rules_status import _HEAD, FakeGitHub, _read

_SCOPE = AuthScope(42, (WS_A,))


@pytest.fixture
def env() -> Iterator[Env]:
    database_url = os.environ.get("TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("set TEST_DATABASE_URL to run PostgreSQL integration tests")
    with portal_schema(database_url, None) as schema:
        yield schema


@pytest.fixture
def factory(env: Env) -> Iterator[async_sessionmaker[Any]]:
    engine = env.engine()
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        asyncio.run(engine.dispose())


class FixedReader:
    """Answers every read with one result, optionally after a step run during the read."""

    def __init__(self, reading: RulesReading, during: Any = None) -> None:
        self.reading = reading
        self.during = during
        self.targets: list[RulesRefreshTarget] = []

    async def read(self, target: RulesRefreshTarget) -> RulesReading:
        self.targets.append(target)
        if self.during is not None:
            await self.during()
        return self.reading


class GitHubReader:
    """The real read of ``tests/test_rules_status.py`` over a mocked GitHub."""

    def __init__(self, github: FakeGitHub) -> None:
        self.github = github

    async def read(self, target: RulesRefreshTarget) -> RulesReading:
        return await asyncio.to_thread(_read, self.github)


def _refresh(factory: async_sessionmaker[Any], reader: Any, now: datetime | None = None) -> int:
    refresher = RefreshRulesStatuses(
        uow_factory=lambda: SqlAlchemyRulesRefreshUnitOfWork(factory),
        reader=reader,
        now=lambda: now or datetime.now(UTC),
    )
    return asyncio.run(refresher.execute())


def _row(factory: async_sessionmaker[Any], repository_id: UUID) -> Any:
    return scalar(
        factory,
        "SELECT status, branch, sha, text, checksum, ignore_masks, diagnostics, defaults_version, "
        "checked_at, refresh_requested_at, refresh_claimed_until FROM repository_rules_status "
        "WHERE repository_id = :id",
        id=repository_id,
    )


def _execute(factory: async_sessionmaker[Any], sql: str, **values: Any) -> None:
    async def run() -> None:
        async with factory() as session:
            await session.execute(text(sql), values)
            await session.commit()

    asyncio.run(run())


@pytest.mark.integration
def test_first_get_is_pending_and_the_worker_read_is_then_returned(env: Env) -> None:
    with api(env, _SCOPE) as (client, factory):
        first = client.get(f"/api/repos/{REPO_A}/rules-status")
        requested = _row(factory, REPO_A)
        again = client.get(f"/api/repos/{REPO_A}/rules-status")
        reader = GitHubReader(FakeGitHub(rules=b"# Rules\n```review-ignore\ndocs/**\n```\n"))
        refreshed = _refresh(factory, reader)
        stored = _row(factory, REPO_A)
        shown = client.get(f"/api/repos/{REPO_A}/rules-status")

    assert first.status_code == 200
    assert first.json() == {
        "status": "pending",
        "branch": "main",
        "path": ".review/rules.md",
        "sha": None,
        "checkedAt": None,
    }
    assert requested.status is None and requested.refresh_requested_at is not None
    # A pending request is not written twice.
    assert again.json()["status"] == "pending"
    assert refreshed == 1
    assert (stored.status, stored.sha, stored.ignore_masks) == ("present", _HEAD, ["docs/**"])
    assert stored.refresh_requested_at is None and stored.refresh_claimed_until is None
    body = shown.json()
    assert body["status"] == "present"
    assert (body["sha"], body["text"], body["ignoreMasks"]) == (
        _HEAD,
        "# Rules\n```review-ignore\ndocs/**\n```\n",
        ["docs/**"],
    )
    assert body["checksum"] == stored.checksum
    # Fresh: the GET after the read requests nothing.
    assert _row(factory, REPO_A).refresh_requested_at is None


@pytest.mark.integration
def test_a_repository_of_another_workspace_is_not_found_and_not_requested(env: Env) -> None:
    with api(env, _SCOPE) as (client, factory):
        other = client.get(f"/api/repos/{REPO_B}/rules-status")
        unknown = client.get(f"/api/repos/{UUID(int=5)}/rules-status")
        rows = scalar(factory, "SELECT count(*) AS n FROM repository_rules_status").n

    assert other.status_code == 404
    assert other.json() == {"detail": "repository not found"}
    assert unknown.status_code == 404
    assert rows == 0


@pytest.mark.integration
def test_missing_is_stored_with_the_defaults_version_and_shown(env: Env) -> None:
    with api(env, _SCOPE) as (client, factory):
        client.get(f"/api/repos/{REPO_A}/rules-status")
        _refresh(factory, GitHubReader(FakeGitHub(rules=None)))
        body = client.get(f"/api/repos/{REPO_A}/rules-status").json()

    assert body["status"] == "missing"
    assert (body["sha"], body["defaultsVersion"]) == (_HEAD, "v1")


@pytest.mark.integration
@pytest.mark.parametrize(
    ("github", "status", "code"),
    [
        (
            FakeGitHub(rules=b"```review-ignore\nsrc/a?.py\n```\n"),
            "invalid",
            "policy_ignore_pattern_invalid",
        ),
        (FakeGitHub(rules=b"x" * 65_537), "invalid", "policy_document_too_large"),
        (
            FakeGitHub(rules=b"x", failures={"/git/trees/" + "2" * 40: 404}),
            "unavailable",
            "policy_revision_unavailable",
        ),
        (
            FakeGitHub(rules=None, review_dir=False, truncated=True),
            "unavailable",
            "policy_revision_unavailable",
        ),
        (
            FakeGitHub(rules=b"x", failures={"/repos/octo/repo": 403}),
            "unavailable",
            "policy_access_denied",
        ),
    ],
    ids=["bad-mask", "too-large", "ambiguous-404", "truncated-tree", "access-denied"],
)
def test_failed_reads_are_stored_with_their_diagnostic(
    env: Env, github: FakeGitHub, status: str, code: str
) -> None:
    with api(env, _SCOPE) as (client, factory):
        client.get(f"/api/repos/{REPO_A}/rules-status")
        _refresh(factory, GitHubReader(github))
        body = client.get(f"/api/repos/{REPO_A}/rules-status").json()

    assert body["status"] == status
    assert [item["code"] for item in body["diagnostics"]] == [code]
    assert body["diagnostics"][0]["retryable"] is False


@pytest.mark.integration
def test_a_stale_or_retryable_result_is_requested_again_on_get(env: Env) -> None:
    transient = RulesReading(
        RulesStatusKind.UNAVAILABLE,
        "main",
        None,
        diagnostics=(RulesDiagnostic("policy_fetch_transient", "m", None, None, True),),
    )
    with api(env, _SCOPE) as (client, factory):
        client.get(f"/api/repos/{REPO_A}/rules-status")
        _refresh(factory, FixedReader(RulesReading(RulesStatusKind.MISSING, "main", _HEAD)))
        fresh = client.get(f"/api/repos/{REPO_A}/rules-status").json()
        after_fresh = _row(factory, REPO_A).refresh_requested_at
        _execute(
            factory,
            "UPDATE repository_rules_status SET checked_at = now() - interval '11 minutes'",
        )
        stale = client.get(f"/api/repos/{REPO_A}/rules-status").json()
        after_stale = _row(factory, REPO_A).refresh_requested_at
        _refresh(factory, FixedReader(transient))
        _execute(
            factory,
            "UPDATE repository_rules_status SET checked_at = now() - interval '31 seconds'",
        )
        failed = client.get(f"/api/repos/{REPO_A}/rules-status").json()
        after_failed = _row(factory, REPO_A).refresh_requested_at

    assert fresh["status"] == "missing" and after_fresh is None
    # The stored result is still returned; the new read is only requested.
    assert stale["status"] == "missing" and after_stale is not None
    assert failed["status"] == "unavailable" and failed["sha"] is None
    assert failed["diagnostics"][0]["retryable"] is True
    assert after_failed is not None


@pytest.mark.integration
def test_a_request_made_during_a_read_survives_it(factory: async_sessionmaker[Any]) -> None:
    async def request_again() -> None:
        async with factory() as session:
            # The new request of a push that arrives while the worker reads.
            await session.execute(
                text("UPDATE repository_rules_status SET refresh_requested_at = clock_timestamp()")
            )
            await session.commit()

    async def request() -> None:
        async with factory() as session:
            await request_rules_refresh(session, REPO_A)
            await session.commit()

    asyncio.run(request())
    missing = RulesReading(RulesStatusKind.MISSING, "main", _HEAD)
    _refresh(factory, FixedReader(missing, during=request_again))
    survived = _row(factory, REPO_A)
    reader = FixedReader(missing)
    _refresh(factory, reader)

    assert survived.status == "missing"
    assert survived.refresh_requested_at is not None
    assert survived.refresh_claimed_until is None
    assert [target.repository_id for target in reader.targets] == [REPO_A]
    assert _row(factory, REPO_A).refresh_requested_at is None


@pytest.mark.integration
def test_a_claimed_request_is_taken_again_only_after_its_lease(
    factory: async_sessionmaker[Any],
) -> None:
    async def request() -> None:
        async with factory() as session:
            await request_rules_refresh(session, REPO_A)
            await session.commit()

    class Crash(Exception):
        pass

    asyncio.run(request())
    now = datetime.now(UTC)

    class Crashing:
        async def read(self, target: RulesRefreshTarget) -> RulesReading:
            raise Crash

    _refresh(factory, Crashing(), now=now)
    within = FixedReader(RulesReading(RulesStatusKind.MISSING, "main", _HEAD))
    _refresh(factory, within, now=now + timedelta(minutes=4))
    after = FixedReader(RulesReading(RulesStatusKind.MISSING, "main", _HEAD))
    _refresh(factory, after, now=now + timedelta(minutes=6))

    assert within.targets == []
    assert [target.repository_id for target in after.targets] == [REPO_A]
    assert _row(factory, REPO_A).status == "missing"


@pytest.mark.integration
def test_onboarding_without_the_file_requests_a_read_that_stores_missing(
    factory: async_sessionmaker[Any],
) -> None:
    async def onboard() -> UUID:
        sync = SyncInstallationRepositories(
            uow_factory=lambda: SqlAlchemyInstallationRepositoriesUnitOfWork(factory),
            rule_sets=load_default_rule_sets(Path("review/rules")),
        )
        result = await sync.execute(
            provider_installation_id=INSTALL_A,
            repositories=(
                RepositoryOnboardingInput(
                    snapshot=RepositorySnapshot(103, "octo/c", "main", "https://github.test/c"),
                    languages={"Python": 100},
                ),
            ),
        )
        return result[0].rule_version.repository_id

    repository_id = asyncio.run(onboard())
    requested = _row(factory, repository_id)
    reader = GitHubReader(FakeGitHub(rules=None, review_dir=False))
    _refresh(factory, reader)
    stored = _row(factory, repository_id)

    assert requested.status is None and requested.refresh_requested_at is not None
    assert (stored.status, stored.sha, stored.defaults_version) == ("missing", _HEAD, "v1")
    assert stored.refresh_requested_at is None


@pytest.mark.integration
def test_a_push_requests_a_read_only_for_a_connected_repository(
    factory: async_sessionmaker[Any],
) -> None:
    request = RequestRulesRefreshOnPush(lambda: SqlAlchemyRulesRefreshUnitOfWork(factory))

    connected = asyncio.run(request.execute(17, 101))
    unknown = asyncio.run(request.execute(17, 999))
    other_installation = asyncio.run(request.execute(18, 101))
    _execute(
        factory, "UPDATE repositories SET installation_available = false WHERE id = :id", id=REPO_B
    )
    removed = asyncio.run(request.execute(18, 102))

    assert (connected, unknown, other_installation, removed) == (True, False, False, False)
    rows = scalar(
        factory,
        "SELECT array_agg(repository_id) AS ids FROM repository_rules_status "
        "WHERE refresh_requested_at IS NOT NULL",
    )
    assert rows.ids == [REPO_A]


@pytest.mark.integration
def test_a_request_wakes_the_worker_on_commit(env: Env, factory: async_sessionmaker[Any]) -> None:
    conninfo = (
        make_url(env.database_url)
        .set(drivername="postgresql")
        .render_as_string(hide_password=False)
    )

    async def scenario() -> list[str]:
        async with await psycopg.AsyncConnection.connect(conninfo, autocommit=True) as listener:
            await listener.execute("LISTEN webhook_work_available")
            async with factory() as session:
                await request_rules_refresh(session, REPO_A)
                await session.commit()
            channels: list[str] = []
            async with asyncio.timeout(5):
                async for notify in listener.notifies():
                    channels.append(notify.channel)
                    break
            return channels

    assert asyncio.run(scenario()) == ["webhook_work_available"]
