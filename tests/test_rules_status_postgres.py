"""Rules status over REST and its durable read requests in PostgreSQL (#150).

Opt-in: ``TEST_DATABASE_URL``. The worker side runs the real stores; GitHub is either
the mocked API of ``tests/test_rules_status.py`` or a fixed reading.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx
import psycopg
import pytest
from alembic.config import Config
from sqlalchemy import Connection, create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DataError, IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker
from sqlalchemy.schema import CreateSchema, DropSchema

from alembic import command
from app.bootstrap.reviews_api import ReviewsApiResources
from app.modules.auth.application.scope import AuthScope
from app.modules.integrations.webhooks.infrastructure.github_installation_tree_provider import (
    StaticGitHubInstallationAccessTokenProvider,
)
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
    SqlAlchemyRulesStatusStore,
    SqlAlchemyRulesStatusUnitOfWork,
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
    raw = b"# Rules\r\n```review-ignore\ndocs/**\n```\n"
    with api(env, _SCOPE) as (client, factory):
        first = client.get(f"/api/repos/{REPO_A}/rules-status")
        requested = _row(factory, REPO_A)
        again = client.get(f"/api/repos/{REPO_A}/rules-status")
        reader = GitHubReader(FakeGitHub(rules=raw))
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
    # SHA-256 of the raw bytes, before the CRLF is normalized.
    assert body["checksum"] == stored.checksum == sha256(raw).hexdigest()
    # Fresh: the GET after the read requests nothing.
    assert _row(factory, REPO_A).refresh_requested_at is None


@pytest.mark.integration
def test_an_empty_file_is_stored_and_shown_as_present(env: Env) -> None:
    with api(env, _SCOPE) as (client, factory):
        client.get(f"/api/repos/{REPO_A}/rules-status")
        _refresh(factory, GitHubReader(FakeGitHub(rules=b"")))
        stored = _row(factory, REPO_A)
        body = client.get(f"/api/repos/{REPO_A}/rules-status").json()

    assert (stored.status, stored.text, stored.ignore_masks) == ("present", "", [])
    assert body["status"] == "present"
    assert (body["text"], body["ignoreMasks"], body["diagnostics"]) == ("", [], [])
    assert body["checksum"] == sha256(b"").hexdigest()


@pytest.mark.integration
@pytest.mark.parametrize("read_before", [False, True], ids=["never-read", "read-before"])
def test_a_repository_the_app_lost_is_access_denied_and_not_requested(
    env: Env, read_before: bool
) -> None:
    with api(env, _SCOPE) as (client, factory):
        if read_before:
            client.get(f"/api/repos/{REPO_A}/rules-status")
            _refresh(factory, FixedReader(RulesReading(RulesStatusKind.MISSING, "main", _HEAD)))
        _execute(
            factory,
            "UPDATE repositories SET installation_available = false WHERE id = :id",
            id=REPO_A,
        )
        repository = client.get(f"/api/repos/{REPO_A}").json()
        response = client.get(f"/api/repos/{REPO_A}/rules-status")
        markers = scalar(
            factory,
            "SELECT count(*) AS n FROM repository_rules_status "
            "WHERE refresh_requested_at IS NOT NULL",
        ).n

    assert repository["installationAvailable"] is False
    assert response.status_code == 200
    body = response.json()
    assert (body["status"], body["sha"]) == ("unavailable", None)
    assert body["diagnostics"] == [
        {
            "code": "policy_access_denied",
            "message": "Нет доступа к документам правил репозитория.",
            "line": None,
            "column": None,
            "retryable": False,
        }
    ]
    assert markers == 0


@pytest.mark.integration
def test_the_portal_store_fails_closed_without_a_scope(factory: async_sessionmaker[Any]) -> None:
    async def scenario() -> None:
        async with factory() as session:
            with pytest.raises(ValueError, match="requires an AuthScope"):
                SqlAlchemyRulesStatusStore(session, None)
            unscoped = SqlAlchemyRulesStatusStore(session, None, allow_unscoped=True)
            # A trusted caller sees the repository of every Workspace.
            assert await unscoped.get(REPO_B) is not None
        with pytest.raises(ValueError, match="requires an AuthScope"):
            SqlAlchemyRulesStatusUnitOfWork(factory)
        async with SqlAlchemyRulesStatusUnitOfWork(factory, allow_unscoped=True) as uow:
            assert await uow.rules_status.get(REPO_B) is not None

    asyncio.run(scenario())


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
def test_missing_without_an_active_rule_version_is_unknown_and_logged(
    env: Env, caplog: pytest.LogCaptureFixture
) -> None:
    with api(env, _SCOPE) as (client, factory):
        _execute(
            factory,
            "UPDATE rule_versions SET is_active = false WHERE repository_id = :id",
            id=REPO_A,
        )
        client.get(f"/api/repos/{REPO_A}/rules-status")
        with caplog.at_level("WARNING"):
            _refresh(factory, FixedReader(RulesReading(RulesStatusKind.MISSING, "main", _HEAD)))
        body = client.get(f"/api/repos/{REPO_A}/rules-status").json()

    assert (body["status"], body["defaultsVersion"]) == ("missing", "unknown")
    assert f"Repository {REPO_A} has no active rule version; defaultsVersion is unknown" in (
        caplog.text
    )


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
            "UPDATE repository_rules_status SET checked_at = now() - interval '29 seconds'",
        )
        within = client.get(f"/api/repos/{REPO_A}/rules-status").json()
        after_within = _row(factory, REPO_A).refresh_requested_at
        _execute(
            factory,
            "UPDATE repository_rules_status SET checked_at = now() - interval '31 seconds'",
        )
        failed = client.get(f"/api/repos/{REPO_A}/rules-status").json()
        after_failed = _row(factory, REPO_A).refresh_requested_at

    assert fresh["status"] == "missing" and after_fresh is None
    assert within["status"] == "unavailable" and after_within is None
    # The stored result is still returned; the new read is only requested.
    assert stale["status"] == "missing" and after_stale is not None
    assert failed["status"] == "unavailable" and failed["sha"] is None
    assert failed["diagnostics"][0]["retryable"] is True
    assert after_failed is not None


@pytest.mark.integration
def test_a_rate_limit_waits_for_its_retry_after_before_a_new_read(env: Env) -> None:
    limited = RulesReading(
        RulesStatusKind.UNAVAILABLE,
        "main",
        None,
        diagnostics=(
            RulesDiagnostic(
                "policy_rate_limited", "m", None, None, True, retry_after=timedelta(seconds=90)
            ),
        ),
    )
    with api(env, _SCOPE) as (client, factory):
        client.get(f"/api/repos/{REPO_A}/rules-status")
        _refresh(factory, FixedReader(limited))
        _execute(
            factory,
            "UPDATE repository_rules_status SET checked_at = now() - interval '89 seconds'",
        )
        within = client.get(f"/api/repos/{REPO_A}/rules-status").json()
        after_within = _row(factory, REPO_A).refresh_requested_at
        _execute(
            factory,
            "UPDATE repository_rules_status SET checked_at = now() - interval '91 seconds'",
        )
        client.get(f"/api/repos/{REPO_A}/rules-status")
        after_pause = _row(factory, REPO_A).refresh_requested_at

    # Retry-After is kept in the row but never shown.
    assert within["diagnostics"][0] == {
        "code": "policy_rate_limited",
        "message": "m",
        "line": None,
        "column": None,
        "retryable": True,
    }
    assert after_within is None
    assert after_pause is not None


async def _request(factory: async_sessionmaker[Any]) -> None:
    async with factory() as session:
        await request_rules_refresh(session, REPO_A)
        await session.commit()


@pytest.mark.integration
def test_a_request_made_during_a_read_survives_it(factory: async_sessionmaker[Any]) -> None:
    async def request_again() -> None:
        # The new request of a push that arrives while the worker reads.
        await _request(factory)

    asyncio.run(_request(factory))
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


def _claim(factory: async_sessionmaker[Any], now: datetime) -> tuple[RulesRefreshTarget, ...]:
    """A claim of a worker that then dies: nothing is saved."""

    async def claim() -> tuple[RulesRefreshTarget, ...]:
        async with SqlAlchemyRulesRefreshUnitOfWork(factory) as uow:
            targets = await uow.rules.claim(now=now, until=now + timedelta(minutes=5), limit=20)
            await uow.commit()
        return targets

    return asyncio.run(claim())


def _save(factory: async_sessionmaker[Any], target: RulesRefreshTarget, reading: Any) -> bool:
    async def save() -> bool:
        async with SqlAlchemyRulesRefreshUnitOfWork(factory) as uow:
            saved = await uow.rules.save(target, reading, checked_at=datetime.now(UTC))
            await uow.commit()
        return saved

    return asyncio.run(save())


@pytest.mark.integration
def test_a_claimed_request_is_taken_again_only_after_its_lease(
    factory: async_sessionmaker[Any],
) -> None:
    asyncio.run(_request(factory))
    now = datetime.now(UTC)

    _claim(factory, now)
    within = FixedReader(RulesReading(RulesStatusKind.MISSING, "main", _HEAD))
    _refresh(factory, within, now=now + timedelta(minutes=4))
    after = FixedReader(RulesReading(RulesStatusKind.MISSING, "main", _HEAD))
    _refresh(factory, after, now=now + timedelta(minutes=6))

    assert within.targets == []
    assert [target.repository_id for target in after.targets] == [REPO_A]
    assert _row(factory, REPO_A).status == "missing"


@pytest.mark.integration
def test_a_read_whose_lease_ended_cannot_overwrite_the_newer_read(
    factory: async_sessionmaker[Any],
) -> None:
    asyncio.run(_request(factory))
    now = datetime.now(UTC)
    (slow,) = _claim(factory, now)
    # The lease of the slow read ends and another worker reads the newer head.
    newer = RulesReading(RulesStatusKind.MISSING, "main", "b" * 40)
    taken_over = FixedReader(newer)
    _refresh(factory, taken_over, now=now + timedelta(minutes=6))

    stale = _save(factory, slow, RulesReading(RulesStatusKind.MISSING, "main", _HEAD))

    assert [target.repository_id for target in taken_over.targets] == [REPO_A]
    assert stale is False
    assert _row(factory, REPO_A).sha == "b" * 40


@pytest.mark.integration
def test_a_read_whose_lease_ended_cannot_save_while_the_new_claim_reads(
    factory: async_sessionmaker[Any],
) -> None:
    asyncio.run(_request(factory))
    now = datetime.now(UTC)
    (slow,) = _claim(factory, now)
    seen: list[tuple[bool, Any]] = []

    async def slow_read_ends() -> None:
        # The slow read A finishes while the read B of the new claim is still running.
        async with SqlAlchemyRulesRefreshUnitOfWork(factory) as uow:
            saved = await uow.rules.save(
                slow,
                RulesReading(RulesStatusKind.MISSING, "main", _HEAD),
                checked_at=datetime.now(UTC),
            )
            await uow.commit()
        async with factory() as session:
            claimed_until = await session.scalar(
                text(
                    "SELECT refresh_claimed_until FROM repository_rules_status "
                    "WHERE repository_id = :id"
                ),
                {"id": REPO_A},
            )
        seen.append((saved, claimed_until))

    newer = RulesReading(RulesStatusKind.MISSING, "main", "b" * 40)
    taken_over = FixedReader(newer, during=slow_read_ends)
    _refresh(factory, taken_over, now=now + timedelta(minutes=6))

    (target,) = taken_over.targets
    assert seen == [(False, target.claimed_until)]
    row = _row(factory, REPO_A)
    assert (row.sha, row.refresh_claimed_until) == ("b" * 40, None)


@pytest.mark.integration
def test_an_unexpected_read_failure_is_stored_as_a_transient_error(
    env: Env,
) -> None:
    class Broken:
        async def read(self, target: RulesRefreshTarget) -> RulesReading:
            raise RuntimeError("bug")

    with api(env, _SCOPE) as (client, factory):
        client.get(f"/api/repos/{REPO_A}/rules-status")
        _refresh(factory, Broken())
        stored = _row(factory, REPO_A)
        body = client.get(f"/api/repos/{REPO_A}/rules-status").json()

    assert stored.refresh_claimed_until is None and stored.refresh_requested_at is None
    assert (body["status"], body["sha"]) == ("unavailable", None)
    assert [(item["code"], item["retryable"]) for item in body["diagnostics"]] == [
        ("policy_fetch_transient", True)
    ]


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


@pytest.mark.integration
def test_the_worker_composition_reads_through_github_and_stores_the_result(env: Env) -> None:
    raw = b"# Rules\n```review-ignore\ndocs/**\n```\n"
    github = FakeGitHub(rules=raw)
    engine = env.engine()
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async def scenario() -> int:
        async with factory() as session:
            await session.execute(
                text("UPDATE repositories SET full_name = 'octo/repo' WHERE id = :id"),
                {"id": REPO_A},
            )
            await request_rules_refresh(session, REPO_A)
            await session.commit()
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(github.respond), base_url="https://api.github.test"
        ) as client:
            refresher = ReviewsApiResources(engine, factory).rules_status_refresher(
                client=client, token_provider=StaticGitHubInstallationAccessTokenProvider("token")
            )
            return await refresher.execute()

    try:
        refreshed = asyncio.run(scenario())
        stored = _row(factory, REPO_A)
    finally:
        asyncio.run(engine.dispose())

    assert refreshed == 1
    assert (stored.status, stored.sha, stored.ignore_masks) == ("present", _HEAD, ["docs/**"])
    assert stored.checksum == sha256(raw).hexdigest()
    assert github.paths[-1] == "/repos/octo/repo/git/blobs/" + "3" * 40


@pytest.fixture
def bare_schema() -> Iterator[Connection]:
    """An empty schema for migrating by hand."""
    database_url = os.environ.get("TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("set TEST_DATABASE_URL to run PostgreSQL integration tests")
    schema = f"test_rules_status_{os.urandom(8).hex()}"
    engine = create_engine(database_url)
    try:
        with engine.connect() as connection:
            connection.execute(CreateSchema(schema))
            connection.execute(text(f'SET search_path TO "{schema}"'))
            connection.commit()
            try:
                yield connection
            finally:
                connection.rollback()
                connection.execute(text("SET search_path TO public"))
                connection.execute(DropSchema(schema, cascade=True))
                connection.commit()
    finally:
        engine.dispose()


def _config(connection: Connection) -> Config:
    config = Config("alembic.ini")
    config.attributes["connection"] = connection
    return config


_TABLE = (
    "SELECT count(*) FROM information_schema.tables "
    "WHERE table_schema = current_schema() AND table_name = 'repository_rules_status'"
)
_INDEX = (
    "SELECT count(*) FROM pg_indexes WHERE schemaname = current_schema() "
    "AND indexname = 'ix_repository_rules_status_refresh'"
)
_REPOSITORY = UUID("00000000-0000-4000-8000-0000000000c1")


def _legacy_repository(connection: Connection) -> None:
    workspace, installation = UUID(int=11), UUID(int=12)
    connection.execute(
        text("INSERT INTO workspaces (id, name, daily_budget_usd) VALUES (:id, 'legacy', 0)"),
        {"id": workspace},
    )
    connection.execute(
        text(
            "INSERT INTO provider_installations (id, workspace_id, provider, external_id, "
            "metadata) VALUES (:id, :workspace, 'github', 17, '{}'::jsonb)"
        ),
        {"id": installation, "workspace": workspace},
    )
    connection.execute(
        text(
            "INSERT INTO repositories (id, provider_installation_id, external_id, full_name, "
            "default_branch, web_url) VALUES (:id, :installation, 101, 'octo/legacy', 'main', "
            "'https://github.test/octo/legacy')"
        ),
        {"id": _REPOSITORY, "installation": installation},
    )


@pytest.mark.integration
def test_migration_0035_creates_the_table_and_its_downgrade_drops_it(
    bare_schema: Connection,
) -> None:
    connection = bare_schema
    config = _config(connection)
    command.upgrade(config, "20261017_0034")
    _legacy_repository(connection)
    connection.commit()

    command.upgrade(config, "20261018_0035")

    assert (connection.scalar(text(_TABLE)), connection.scalar(text(_INDEX))) == (1, 1)
    # An existing repository has no row: its first GET answers pending and requests a read.
    assert connection.scalar(text("SELECT count(*) FROM repository_rules_status")) == 0
    connection.execute(
        text(
            "INSERT INTO repository_rules_status (repository_id, refresh_requested_at) "
            "VALUES (:id, now())"
        ),
        {"id": _REPOSITORY},
    )
    connection.commit()
    command.downgrade(config, "20261017_0034")
    assert (connection.scalar(text(_TABLE)), connection.scalar(text(_INDEX))) == (0, 0)
    assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "20261017_0034"
    connection.commit()
    command.upgrade(config, "head")
    # The stored results and requests are lost; the table comes back empty.
    assert connection.scalar(text("SELECT count(*) FROM repository_rules_status")) == 0


_VALID = {
    "status": "present",
    "branch": "main",
    "sha": "a" * 40,
    "text": "",
    "checksum": sha256(b"").hexdigest(),
    "ignore_masks": "[]",
    "diagnostics": "[]",
    "defaults_version": None,
    "checked_at": "2026-10-18 12:00:00+00",
}
_DIAGNOSTIC = '[{"code": "policy_fetch_transient", "message": "m", "retryable": true}]'


@pytest.mark.integration
@pytest.mark.parametrize(
    ("change", "constraint"),
    [
        ({"status": "pending"}, "ck_repository_rules_status_status"),
        ({"checked_at": None}, "ck_repository_rules_status_read_complete"),
        ({"sha": "a" * 39}, "ck_repository_rules_status_sha"),
        ({"sha": "A" * 40}, "ck_repository_rules_status_sha"),
        ({"sha": "a" * 64}, "value too long"),
        ({"checksum": "a" * 63 + "g"}, "ck_repository_rules_status_checksum"),
        ({"ignore_masks": None}, "ck_repository_rules_status_present"),
        ({"status": "missing"}, "ck_repository_rules_status_missing"),
        ({"status": "invalid"}, "ck_repository_rules_status_diagnosed"),
        ({"status": "unavailable", "sha": None}, "ck_repository_rules_status_diagnosed"),
    ],
    ids=[
        "unknown-status",
        "read-without-time",
        "short-sha",
        "upper-case-sha",
        "sha-256",
        "bad-checksum",
        "present-without-masks",
        "missing-without-defaults",
        "invalid-without-diagnostic",
        "unavailable-without-diagnostic",
    ],
)
def test_the_row_constraints_refuse_an_incomplete_read(
    bare_schema: Connection, change: dict[str, Any], constraint: str
) -> None:
    connection = bare_schema
    command.upgrade(_config(connection), "head")
    _legacy_repository(connection)
    insert = text(
        "INSERT INTO repository_rules_status (repository_id, status, branch, sha, text, "
        "checksum, ignore_masks, diagnostics, defaults_version, checked_at) VALUES (:id, "
        ":status, :branch, :sha, :text, :checksum, CAST(:ignore_masks AS jsonb), "
        "CAST(:diagnostics AS jsonb), :defaults_version, CAST(:checked_at AS timestamptz))"
    )
    with connection.begin_nested() as valid:
        connection.execute(insert, {"id": _REPOSITORY, **_VALID})
        valid.rollback()
    with connection.begin_nested() as unavailable:
        # A failed read without a revision is complete with its diagnostic.
        connection.execute(
            insert,
            {"id": _REPOSITORY, **_VALID, "status": "unavailable", "sha": None}
            | {"text": None, "checksum": None, "ignore_masks": None, "diagnostics": _DIAGNOSTIC},
        )
        unavailable.rollback()

    # A 64 hex SHA is refused by the column length before the CHECK runs.
    with pytest.raises((IntegrityError, DataError), match=constraint):
        connection.execute(insert, {"id": _REPOSITORY, **_VALID, **change})
