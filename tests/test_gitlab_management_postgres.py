"""Real PostgreSQL atomicity and concurrency for trusted GitLab management."""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID, uuid4

import httpx
import psycopg
import pytest
from alembic.config import Config
from sqlalchemy import CursorResult, create_engine, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.schema import CreateSchema, DropSchema

from alembic import command
from app.modules.integrations.gitlab.application.manage_connection import (
    GitLabConnectionLifecycle,
    GitLabManagementError,
    GitLabOriginPolicy,
    ManageGitLabConnection,
)
from app.modules.integrations.gitlab.infrastructure.connection_store import (
    CredentialCipherAdapter,
    SqlAlchemyGitLabManagementUnitOfWork,
)
from app.modules.integrations.gitlab.infrastructure.credential_cipher import CredentialCipher
from app.modules.integrations.gitlab.infrastructure.project_details import GitLabProjectDetails
from app.modules.repositories.application.installation_repositories import RepositorySnapshot
from app.modules.repositories.application.onboard_repository import load_default_rule_sets
from app.modules.repositories.application.sync_installation_repositories import (
    RepositoryOnboardingInput,
)

pytestmark = pytest.mark.integration
_ORIGIN = "https://gitlab.com"
_TOKEN = "synthetic-test-token"


@pytest.fixture
def management_database() -> Iterator[tuple[str, str, UUID]]:
    database_url = os.environ.get("TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("set TEST_DATABASE_URL to run PostgreSQL integration tests")
    schema = f"test_gitlab_management_{uuid4().hex}"
    workspace = uuid4()
    engine = create_engine(database_url, hide_parameters=True)
    try:
        with engine.connect() as connection:
            connection.execute(CreateSchema(schema))
            connection.execute(text(f'SET search_path TO "{schema}"'))
            connection.commit()
            config = Config("alembic.ini")
            config.attributes["connection"] = connection
            command.upgrade(config, "head")
            connection.execute(
                text(
                    "INSERT INTO workspaces (id, name, daily_budget_usd) VALUES (:id, "
                    "'existing workspace', 7)"
                ),
                {"id": workspace},
            )
            connection.execute(
                text(
                    "INSERT INTO prompt_versions (id, key, version, content, checksum, "
                    "is_active) VALUES (:id, 'review.system', 1, 'seeded system prompt', "
                    ":checksum, true)"
                ),
                {"id": uuid4(), "checksum": "a" * 64},
            )
            connection.commit()
            try:
                yield database_url, schema, workspace
            finally:
                connection.rollback()
                connection.execute(text("SET search_path TO public"))
                connection.execute(DropSchema(schema, cascade=True))
                connection.commit()
    finally:
        engine.dispose()


@dataclass
class Projects:
    origin: str = _ORIGIN

    async def authenticate(self, *, origin: str, token: str) -> None:
        assert origin == self.origin
        assert token == _TOKEN

    async def fetch_project(
        self, *, origin: str, token: str, project_id: int
    ) -> RepositoryOnboardingInput:
        await self.authenticate(origin=origin, token=token)
        return RepositoryOnboardingInput(
            RepositorySnapshot(
                project_id, "parent/nested/project", "main", f"{origin}/parent/nested/project"
            ),
            {"Python": 4990, "TypeScript": 5010},
        )


def test_real_codec_create_bind_initializes_runnable_defaults_and_existing_budget(
    management_database: tuple[str, str, UUID],
) -> None:
    database_url, schema, workspace = management_database
    installation = uuid4()

    async def exercise() -> None:
        engine = create_async_engine(
            database_url, connect_args={"options": f"-csearch_path={schema}"}, hide_parameters=True
        )
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        management = ManageGitLabConnection(
            uow_factory=lambda: SqlAlchemyGitLabManagementUnitOfWork(sessions),
            codec=CredentialCipherAdapter(
                CredentialCipher(active_key_id="test-key", keys={"test-key": b"k" * 32})
            ),
            projects=Projects(),
            origin_policy=GitLabOriginPolicy(frozenset({_ORIGIN})),
            rule_sets=load_default_rule_sets(Path("review/rules")),
        )
        try:
            result = await management.create(
                installation_id=installation,
                workspace_id=workspace,
                instance_origin=_ORIGIN,
                token=_TOKEN,
                webhook_secret="s" * 32,
            )
            assert result.generation == 1
            assert result.intake_enabled is False
            bound = await management.bind_project(installation_id=installation, project_id=42)
            async with sessions() as session:
                row = (
                    await session.execute(
                        text(
                            "SELECT external_id, full_name, default_branch, web_url, enabled,"
                            " installation_available, default_engine, wait_for_ci, "
                            "rag_enabled, review_event, max_comments, branch_filter, "
                            "severity_threshold FROM repositories WHERE id=:id"
                        ),
                        {"id": bound.repository_id},
                    )
                ).one()
                assert tuple(row) == (
                    42,
                    "parent/nested/project",
                    "main",
                    "https://gitlab.com/parent/nested/project",
                    True,
                    True,
                    "fast",
                    "never",
                    False,
                    "comment",
                    10,
                    [],
                    "all",
                )
                assert (
                    await session.scalar(
                        text("SELECT daily_budget_usd FROM workspaces WHERE id=:id"),
                        {"id": workspace},
                    )
                    == 7
                )
                assert (
                    await session.scalar(
                        text(
                            "SELECT count(*) FROM rule_versions WHERE repository_id=:id AND "
                            "is_active"
                        ),
                        {"id": bound.repository_id},
                    )
                    == 1
                )
                rules = await session.scalar(
                    text("SELECT rules FROM rule_versions WHERE repository_id=:id AND is_active"),
                    {"id": bound.repository_id},
                )
                assert isinstance(rules, list)
                assert rules[0]["include"] == ["src/**/*.{ts,tsx}"]
                assert (
                    await session.scalar(text("SELECT count(*) FROM gitlab_project_bindings")) == 1
                )
                assert (
                    await session.scalar(text("SELECT token_ciphertext FROM gitlab_connections"))
                    != _TOKEN.encode()
                )
                assert (
                    await session.scalar(text("SELECT count(*) FROM github_user_workspace_access"))
                    == 0
                )
        finally:
            await engine.dispose()

    asyncio.run(exercise())


def _management(
    sessions: async_sessionmaker[AsyncSession], projects: Projects | None = None
) -> ManageGitLabConnection:
    return ManageGitLabConnection(
        uow_factory=lambda: SqlAlchemyGitLabManagementUnitOfWork(sessions),
        codec=CredentialCipherAdapter(
            CredentialCipher(active_key_id="test-key", keys={"test-key": b"k" * 32})
        ),
        projects=projects or Projects(),
        origin_policy=GitLabOriginPolicy(frozenset({_ORIGIN, "https://gitlab.example.com"})),
        rule_sets=load_default_rule_sets(Path("review/rules")),
    )


async def _create(
    management: ManageGitLabConnection,
    workspace: UUID,
    *,
    origin: str = _ORIGIN,
    installation: UUID | None = None,
) -> UUID:
    installation = installation or uuid4()
    await management.create(
        installation_id=installation,
        workspace_id=workspace,
        instance_origin=origin,
        token=_TOKEN,
        webhook_secret="s" * 32,
    )
    return installation


def test_prompt_failure_has_stable_code_and_rolls_back_repository_reservation_and_rules(
    management_database: tuple[str, str, UUID],
) -> None:
    from app.modules.integrations.gitlab.application.manage_connection import GitLabManagementError

    database_url, schema, workspace = management_database

    async def exercise() -> None:
        engine = create_async_engine(
            database_url, connect_args={"options": f"-csearch_path={schema}"}, hide_parameters=True
        )
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        try:
            management = _management(sessions)
            installation = await _create(management, workspace)
            async with sessions.begin() as session:
                await session.execute(text("UPDATE prompt_versions SET is_active=false"))
            with pytest.raises(GitLabManagementError) as failure:
                await management.bind_project(installation_id=installation, project_id=42)
            assert failure.value.code == "prompt_not_seeded"
            assert "uv run python -m app.bootstrap.seed_prompts" in str(failure.value)
            async with sessions() as session:
                assert await session.scalar(text("SELECT count(*) FROM repositories")) == 0
                assert (
                    await session.scalar(text("SELECT count(*) FROM gitlab_project_bindings")) == 0
                )
                assert await session.scalar(text("SELECT count(*) FROM rule_versions")) == 0
                assert (
                    await session.scalar(
                        text("SELECT count(*) FROM prompt_versions WHERE is_active")
                    )
                    == 0
                )
        finally:
            await engine.dispose()

    asyncio.run(exercise())


def test_repeated_create_and_bind_preserve_credentials_preferences_and_rule_history(
    management_database: tuple[str, str, UUID],
) -> None:
    database_url, schema, workspace = management_database

    async def exercise() -> None:
        engine = create_async_engine(
            database_url, connect_args={"options": f"-csearch_path={schema}"}, hide_parameters=True
        )
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        try:
            management = _management(sessions)
            installation = await _create(management, workspace)
            first = await management.bind_project(installation_id=installation, project_id=42)
            async with sessions.begin() as session:
                original_credential = (
                    await session.execute(
                        text(
                            "SELECT token_ciphertext, credential_key_id, "
                            "webhook_secret_digest FROM gitlab_connections"
                        )
                    )
                ).one()
                await session.execute(
                    text(
                        "UPDATE repositories SET enabled=false, default_engine='deep', "
                        "wait_for_ci='always', rag_enabled=true, max_comments=3, "
                        "branch_filter=ARRAY['release/*'], severity_threshold='critical' "
                        "WHERE id=:id"
                    ),
                    {"id": first.repository_id},
                )
                await session.execute(
                    text("UPDATE rule_versions SET is_active=false WHERE repository_id=:id"),
                    {"id": first.repository_id},
                )
                await session.execute(
                    text(
                        "INSERT INTO rule_versions (id, repository_id, version, rules, "
                        'checksum, is_active) VALUES (:id, :repository, 2, \'[{"name": '
                        '"custom"}]\', :checksum, true)'
                    ),
                    {"id": uuid4(), "repository": first.repository_id, "checksum": "c" * 64},
                )
            await management.create(
                installation_id=installation,
                workspace_id=workspace,
                instance_origin=_ORIGIN,
                token="different-synthetic-token",
                webhook_secret="x" * 32,
            )
            repeated = await management.bind_project(installation_id=installation, project_id=42)
            assert repeated.repository_id == first.repository_id
            assert repeated.created is False
            assert repeated.rules_created is False
            async with sessions() as session:
                assert tuple(
                    (
                        await session.execute(
                            text(
                                "SELECT token_ciphertext, credential_key_id, "
                                "webhook_secret_digest FROM gitlab_connections"
                            )
                        )
                    ).one()
                ) == tuple(original_credential)
                assert tuple(
                    (
                        await session.execute(
                            text(
                                "SELECT enabled, default_engine, wait_for_ci, rag_enabled, "
                                "max_comments, branch_filter, severity_threshold FROM "
                                "repositories"
                            )
                        )
                    ).one()
                ) == (False, "deep", "always", True, 3, ["release/*"], "critical")
                assert await session.scalar(text("SELECT count(*) FROM rule_versions")) == 2
                assert await session.scalar(
                    text("SELECT rules FROM rule_versions WHERE is_active")
                ) == [{"name": "custom"}]
                assert (
                    await session.scalar(
                        text("SELECT credential_generation FROM gitlab_connections")
                    )
                    == 1
                )
        finally:
            await engine.dispose()

    asyncio.run(exercise())


def test_concurrent_two_workspace_claims_are_exclusive_and_loser_has_no_partial_repository(
    management_database: tuple[str, str, UUID],
) -> None:
    from app.modules.integrations.gitlab.application.manage_connection import (
        BindingResult,
        GitLabManagementError,
    )

    database_url, schema, workspace = management_database

    async def exercise() -> None:
        engine = create_async_engine(
            database_url, connect_args={"options": f"-csearch_path={schema}"}, hide_parameters=True
        )
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        other_workspace = uuid4()
        try:
            async with sessions.begin() as session:
                await session.execute(
                    text(
                        "INSERT INTO workspaces (id, name, daily_budget_usd) VALUES (:id, "
                        "'second workspace', 13)"
                    ),
                    {"id": other_workspace},
                )
            management = _management(sessions)
            first = await _create(management, workspace)
            second = await _create(management, other_workspace)
            arrived = 0
            ready = asyncio.Event()

            class ConcurrentProjects(Projects):
                async def fetch_project(
                    self, *, origin: str, token: str, project_id: int
                ) -> RepositoryOnboardingInput:
                    nonlocal arrived
                    arrived += 1
                    if arrived == 2:
                        ready.set()
                    await asyncio.wait_for(ready.wait(), timeout=10)
                    return await super().fetch_project(
                        origin=origin, token=token, project_id=project_id
                    )

            concurrent = _management(sessions, ConcurrentProjects())
            outcomes = await asyncio.wait_for(
                asyncio.gather(
                    concurrent.bind_project(installation_id=first, project_id=42),
                    concurrent.bind_project(installation_id=second, project_id=42),
                    return_exceptions=True,
                ),
                timeout=15,
            )
            assert sum(isinstance(item, BindingResult) for item in outcomes) == 1
            failures = [item for item in outcomes if isinstance(item, GitLabManagementError)]
            assert len(failures) == 1
            assert failures[0].code == "project_reserved"
            async with sessions() as session:
                assert await session.scalar(text("SELECT count(*) FROM repositories")) == 1
                assert await session.scalar(text("SELECT count(*) FROM rule_versions")) == 1
                assert (
                    await session.scalar(text("SELECT count(*) FROM gitlab_project_bindings")) == 1
                )
                assert (
                    await session.scalar(
                        text("SELECT daily_budget_usd FROM workspaces WHERE id=:id"),
                        {"id": other_workspace},
                    )
                    == 13
                )
        finally:
            await engine.dispose()

    asyncio.run(exercise())


@pytest.mark.parametrize("race", ["generation", "retired", "binding"])
def test_provider_gap_revalidates_generation_retirement_and_binding_snapshot(
    management_database: tuple[str, str, UUID], race: str
) -> None:
    from app.modules.integrations.gitlab.application.manage_connection import GitLabManagementError

    database_url, schema, workspace = management_database

    async def exercise() -> None:
        engine = create_async_engine(
            database_url, connect_args={"options": f"-csearch_path={schema}"}, hide_parameters=True
        )
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        try:
            management = _management(sessions)
            installation = await _create(management, workspace)

            class RacingProjects(Projects):
                async def fetch_project(
                    self, *, origin: str, token: str, project_id: int
                ) -> RepositoryOnboardingInput:
                    if race == "binding":
                        await management.bind_project(installation_id=installation, project_id=99)
                    else:
                        async with sessions.begin() as session:
                            if race == "generation":
                                await session.execute(
                                    text(
                                        "UPDATE gitlab_connections SET "
                                        "credential_generation=credential_generation+1 WHERE "
                                        "provider_installation_id=:id"
                                    ),
                                    {"id": installation},
                                )
                            else:
                                await session.execute(
                                    text(
                                        "UPDATE provider_installations SET "
                                        "deleted_at=clock_timestamp() WHERE id=:id"
                                    ),
                                    {"id": installation},
                                )
                    return await super().fetch_project(
                        origin=origin, token=token, project_id=project_id
                    )

            racing = _management(sessions, RacingProjects())
            with pytest.raises(GitLabManagementError, match="connection_changed"):
                await racing.bind_project(installation_id=installation, project_id=42)
            async with sessions() as session:
                assert (
                    await session.scalar(
                        text("SELECT count(*) FROM repositories WHERE external_id=42")
                    )
                    == 0
                )
                assert (
                    await session.scalar(
                        text(
                            "SELECT count(*) FROM gitlab_project_bindings WHERE "
                            "project_external_id=42"
                        )
                    )
                    == 0
                )
        finally:
            await engine.dispose()

    asyncio.run(exercise())


def test_onboarding_failure_rolls_back_reservation_repository_and_rule_version(
    management_database: tuple[str, str, UUID],
) -> None:
    from sqlalchemy.exc import IntegrityError

    database_url, schema, workspace = management_database

    async def exercise() -> None:
        engine = create_async_engine(
            database_url, connect_args={"options": f"-csearch_path={schema}"}, hide_parameters=True
        )
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        try:
            management = _management(sessions)
            installation = await _create(management, workspace)
            async with sessions.begin() as session:
                await session.execute(
                    text(
                        "ALTER TABLE rule_versions ADD CONSTRAINT ck_test_reject_rules CHECK "
                        "(jsonb_array_length(rules) = 0)"
                    )
                )
            with pytest.raises(IntegrityError):
                await management.bind_project(installation_id=installation, project_id=42)
            async with sessions() as session:
                assert await session.scalar(text("SELECT count(*) FROM repositories")) == 0
                assert (
                    await session.scalar(text("SELECT count(*) FROM gitlab_project_bindings")) == 0
                )
                assert await session.scalar(text("SELECT count(*) FROM rule_versions")) == 0
                assert await session.scalar(text("SELECT count(*) FROM gitlab_connections")) == 1
        finally:
            await engine.dispose()

    asyncio.run(exercise())


def test_distinct_origins_allow_same_project_numbers_without_cross_tenant_conflict(
    management_database: tuple[str, str, UUID],
) -> None:
    database_url, schema, workspace = management_database

    async def exercise() -> None:
        engine = create_async_engine(
            database_url, connect_args={"options": f"-csearch_path={schema}"}, hide_parameters=True
        )
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        try:
            other_workspace = uuid4()
            async with sessions.begin() as session:
                await session.execute(
                    text(
                        "INSERT INTO workspaces (id, name, daily_budget_usd) VALUES (:id, "
                        "'other origin', 19)"
                    ),
                    {"id": other_workspace},
                )
            first = _management(sessions)
            other = _management(sessions, Projects("https://gitlab.example.com"))
            first_id = await _create(first, workspace)
            other_id = await _create(other, other_workspace, origin="https://gitlab.example.com")
            one = await first.bind_project(installation_id=first_id, project_id=42)
            two = await other.bind_project(installation_id=other_id, project_id=42)
            assert one.repository_id != two.repository_id
            async with sessions() as session:
                origins = (
                    await session.execute(
                        text(
                            "SELECT instance_origin, project_external_id FROM "
                            "gitlab_project_bindings ORDER BY instance_origin"
                        )
                    )
                ).all()
                assert [tuple(row) for row in origins] == [
                    ("https://gitlab.com", 42),
                    ("https://gitlab.example.com", 42),
                ]
        finally:
            await engine.dispose()

    asyncio.run(exercise())


def test_existing_workspace_required_and_failed_token_never_persists_connection(
    management_database: tuple[str, str, UUID],
) -> None:
    from app.modules.integrations.gitlab.application.manage_connection import GitLabManagementError

    database_url, schema, workspace = management_database

    async def exercise() -> None:
        engine = create_async_engine(
            database_url, connect_args={"options": f"-csearch_path={schema}"}, hide_parameters=True
        )
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        calls = 0

        class RejectedProjects(Projects):
            async def authenticate(self, *, origin: str, token: str) -> None:
                nonlocal calls
                calls += 1
                raise GitLabManagementError("invalid_token")

        try:
            management = _management(sessions, RejectedProjects())
            with pytest.raises(GitLabManagementError, match="workspace_not_found"):
                await _create(management, uuid4())
            assert calls == 0
            with pytest.raises(GitLabManagementError, match="invalid_token"):
                await _create(management, workspace)
            assert calls == 1
            async with sessions() as session:
                assert (
                    await session.scalar(text("SELECT count(*) FROM provider_installations")) == 0
                )
                assert await session.scalar(text("SELECT count(*) FROM gitlab_connections")) == 0
                assert (
                    await session.scalar(
                        text("SELECT daily_budget_usd FROM workspaces WHERE id=:id"),
                        {"id": workspace},
                    )
                    == 7
                )
        finally:
            await engine.dispose()

    asyncio.run(exercise())


def test_provider_calls_have_no_open_real_transaction(
    management_database: tuple[str, str, UUID],
) -> None:
    from sqlalchemy import Connection, event

    database_url, schema, workspace = management_database

    async def exercise() -> None:
        engine = create_async_engine(
            database_url, connect_args={"options": f"-csearch_path={schema}"}, hide_parameters=True
        )
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        active_transactions: set[Connection] = set()

        def began(connection: Connection) -> None:
            active_transactions.add(connection)

        def ended(connection: Connection) -> None:
            active_transactions.remove(connection)

        event.listen(engine.sync_engine, "begin", began)
        event.listen(engine.sync_engine, "commit", ended)
        event.listen(engine.sync_engine, "rollback", ended)

        class CheckedProjects(Projects):
            async def authenticate(self, *, origin: str, token: str) -> None:
                assert not active_transactions
                await super().authenticate(origin=origin, token=token)

        try:
            management = _management(sessions, CheckedProjects())
            installation = await _create(management, workspace)
            await management.bind_project(installation_id=installation, project_id=42)
            assert not active_transactions
        finally:
            await engine.dispose()

    asyncio.run(exercise())


def test_create_uuid_identity_conflicts_are_safe_and_revoked_origin_is_not_recreated(
    management_database: tuple[str, str, UUID],
) -> None:
    from app.modules.integrations.gitlab.application.manage_connection import GitLabManagementError

    database_url, schema, workspace = management_database

    async def exercise() -> None:
        engine = create_async_engine(
            database_url, connect_args={"options": f"-csearch_path={schema}"}, hide_parameters=True
        )
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        try:
            management = _management(sessions)
            installation = await _create(management, workspace)
            other_workspace = uuid4()
            async with sessions.begin() as session:
                await session.execute(
                    text(
                        "INSERT INTO workspaces (id, name, daily_budget_usd) VALUES (:id, "
                        "'other', 0)"
                    ),
                    {"id": other_workspace},
                )
            with pytest.raises(GitLabManagementError, match="installation_identity_conflict"):
                await _create(management, other_workspace, installation=installation)
            with pytest.raises(GitLabManagementError, match="installation_identity_conflict"):
                await _create(
                    management,
                    workspace,
                    installation=installation,
                    origin="https://gitlab.example.com",
                )
            async with sessions.begin() as session:
                await session.execute(
                    text(
                        "UPDATE gitlab_connections SET revoked_at=clock_timestamp(), "
                        "token_ciphertext=NULL, credential_key_id=NULL, "
                        "credential_generation=2 WHERE provider_installation_id=:id"
                    ),
                    {"id": installation},
                )
            result = await management.create(
                installation_id=installation,
                workspace_id=workspace,
                instance_origin=_ORIGIN,
                token="unused-synthetic",
                webhook_secret="u" * 32,
            )
            assert result.revoked is True
            assert result.generation == 2
            with pytest.raises(GitLabManagementError, match="connection_revoked"):
                await management.bind_project(installation_id=installation, project_id=42)
            async with sessions() as session:
                assert (
                    await session.scalar(text("SELECT token_ciphertext FROM gitlab_connections"))
                    is None
                )
                assert (
                    await session.scalar(text("SELECT count(*) FROM provider_installations")) == 1
                )
        finally:
            await engine.dispose()

    asyncio.run(exercise())


class ReplacementProjects(Projects):
    async def authenticate(self, *, origin: str, token: str) -> None:
        assert origin == self.origin
        assert token in {_TOKEN, "replacement-token"}


def _lifecycle(
    sessions: async_sessionmaker[AsyncSession],
    projects: Projects | None = None,
) -> object:
    from app.modules.integrations.gitlab.application.manage_connection import (
        GitLabConnectionLifecycle,
    )

    return GitLabConnectionLifecycle(
        uow_factory=lambda: SqlAlchemyGitLabManagementUnitOfWork(sessions),
        codec=CredentialCipherAdapter(
            CredentialCipher(active_key_id="replacement-key", keys={"replacement-key": b"n" * 32})
        ),
        projects=projects or ReplacementProjects(),
        origin_policy=GitLabOriginPolicy(frozenset({_ORIGIN, "https://gitlab.example.com"})),
    )


def test_rotation_real_cipher_needs_no_old_key_and_preserves_settings_digest_and_rules(
    management_database: tuple[str, str, UUID],
) -> None:
    from app.modules.integrations.gitlab.application.manage_connection import (
        GitLabConnectionLifecycle,
    )

    database_url, schema, workspace = management_database

    async def exercise() -> None:
        engine = create_async_engine(
            database_url, connect_args={"options": f"-csearch_path={schema}"}, hide_parameters=True
        )
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        try:
            management = _management(sessions)
            installation = await _create(management, workspace)
            bound = await management.bind_project(installation_id=installation, project_id=42)
            await management.bind_project(installation_id=installation, project_id=99)
            async with sessions.begin() as session:
                await session.execute(text("UPDATE repositories SET max_comments=3, enabled=false"))
                before = (
                    await session.execute(
                        text(
                            "SELECT webhook_secret_digest, token_ciphertext FROM gitlab_connections"
                        )
                    )
                ).one()
            lifecycle = _lifecycle(sessions)
            assert isinstance(lifecycle, GitLabConnectionLifecycle)
            result = await lifecycle.rotate_token(
                installation_id=installation, token="replacement-token", expected_generation=1
            )
            assert result.generation == 2
            inspection = await GitLabConnectionLifecycle(
                uow_factory=lambda: SqlAlchemyGitLabManagementUnitOfWork(sessions)
            ).inspect(installation_id=installation)
            assert inspection.connection == result
            assert len(inspection.repositories) == 2
            assert all(item.rules_ready and not item.enabled for item in inspection.repositories)
            assert inspection.prompt_ready is True
            async with sessions() as session:
                row = (
                    await session.execute(
                        text(
                            "SELECT webhook_secret_digest, token_ciphertext, credential_key_id "
                            "FROM gitlab_connections"
                        )
                    )
                ).one()
                assert row[0] == before[0]
                assert row[1] != before[1]
                assert row[2] == "replacement-key"
                assert await session.scalar(text("SELECT count(*) FROM rule_versions")) == 2
                assert (
                    await session.scalar(
                        text("SELECT max_comments FROM repositories WHERE id=:id"),
                        {"id": bound.repository_id},
                    )
                    == 3
                )
            replacement = ManageGitLabConnection(
                uow_factory=lambda: SqlAlchemyGitLabManagementUnitOfWork(sessions),
                codec=CredentialCipherAdapter(
                    CredentialCipher(
                        active_key_id="replacement-key", keys={"replacement-key": b"n" * 32}
                    )
                ),
                projects=ReplacementProjects(),
                origin_policy=GitLabOriginPolicy(frozenset({_ORIGIN})),
                rule_sets=load_default_rule_sets(Path("review/rules")),
            )
            assert (
                await replacement.bind_project(installation_id=installation, project_id=43)
            ).created is True
        finally:
            await engine.dispose()

    asyncio.run(exercise())


@pytest.mark.parametrize("race", ["bind", "rotate", "revoke"])
def test_rotation_provider_gap_refuses_concurrent_management_changes(
    management_database: tuple[str, str, UUID],
    race: str,
) -> None:
    from app.modules.integrations.gitlab.application.manage_connection import (
        GitLabConnectionLifecycle,
        GitLabManagementError,
    )

    database_url, schema, workspace = management_database

    async def exercise() -> None:
        engine = create_async_engine(
            database_url, connect_args={"options": f"-csearch_path={schema}"}, hide_parameters=True
        )
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        try:
            management = _management(sessions)
            installation = await _create(management, workspace)
            await management.bind_project(installation_id=installation, project_id=42)
            changed = False

            class RacingProjects(ReplacementProjects):
                async def fetch_project(
                    self, *, origin: str, token: str, project_id: int
                ) -> RepositoryOnboardingInput:
                    nonlocal changed
                    if not changed:
                        changed = True
                        if race == "bind":
                            await management.bind_project(
                                installation_id=installation, project_id=99
                            )
                        elif race == "rotate":
                            inner = _lifecycle(sessions)
                            assert isinstance(inner, GitLabConnectionLifecycle)
                            await inner.rotate_token(
                                installation_id=installation, token="replacement-token"
                            )
                        else:
                            await GitLabConnectionLifecycle(
                                uow_factory=lambda: SqlAlchemyGitLabManagementUnitOfWork(sessions)
                            ).revoke_installation(installation_id=installation)
                    return await super().fetch_project(
                        origin=origin, token=token, project_id=project_id
                    )

            lifecycle = _lifecycle(sessions, RacingProjects())
            assert isinstance(lifecycle, GitLabConnectionLifecycle)
            with pytest.raises(GitLabManagementError, match="connection_changed"):
                await lifecycle.rotate_token(
                    installation_id=installation, token="replacement-token"
                )
            async with sessions() as session:
                row = (
                    await session.execute(
                        text(
                            "SELECT credential_generation, credential_key_id, revoked_at "
                            "IS NOT NULL "
                            "FROM gitlab_connections"
                        )
                    )
                ).one()
                assert (
                    tuple(row)
                    == {
                        "bind": (1, "test-key", False),
                        "rotate": (2, "replacement-key", False),
                        "revoke": (2, None, True),
                    }[race]
                )
                assert await session.scalar(
                    text("SELECT count(*) FROM gitlab_project_bindings")
                ) == (2 if race == "bind" else 0 if race == "revoke" else 1)
        finally:
            await engine.dispose()

    asyncio.run(exercise())


@pytest.mark.parametrize("race", ["rotate", "revoke"])
def test_bind_provider_gap_cannot_restore_rotated_or_revoked_credentials(
    management_database: tuple[str, str, UUID],
    race: str,
) -> None:
    from app.modules.integrations.gitlab.application.manage_connection import (
        GitLabConnectionLifecycle,
        GitLabManagementError,
    )

    database_url, schema, workspace = management_database

    async def exercise() -> None:
        engine = create_async_engine(
            database_url, connect_args={"options": f"-csearch_path={schema}"}, hide_parameters=True
        )
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        try:
            installation = await _create(_management(sessions), workspace)

            class RacingProjects(Projects):
                async def fetch_project(
                    self, *, origin: str, token: str, project_id: int
                ) -> RepositoryOnboardingInput:
                    lifecycle = _lifecycle(sessions)
                    assert isinstance(lifecycle, GitLabConnectionLifecycle)
                    if race == "rotate":
                        await lifecycle.rotate_token(
                            installation_id=installation, token="replacement-token"
                        )
                    else:
                        await lifecycle.revoke_installation(installation_id=installation)
                    return await super().fetch_project(
                        origin=origin, token=token, project_id=project_id
                    )

            with pytest.raises(GitLabManagementError, match="connection_changed"):
                await _management(sessions, RacingProjects()).bind_project(
                    installation_id=installation, project_id=42
                )
            async with sessions() as session:
                assert await session.scalar(text("SELECT count(*) FROM repositories")) == 0
                assert (
                    await session.scalar(text("SELECT count(*) FROM gitlab_project_bindings")) == 0
                )
                assert (
                    await session.scalar(
                        text("SELECT credential_generation FROM gitlab_connections")
                    )
                    == 2
                )
        finally:
            await engine.dispose()

    asyncio.run(exercise())


@pytest.mark.parametrize("failure", ["authentication", "project", "expected_generation"])
def test_invalid_replacement_leaves_persisted_connection_unchanged(
    management_database: tuple[str, str, UUID],
    failure: str,
) -> None:
    import psycopg

    from app.modules.integrations.gitlab.application.manage_connection import (
        GitLabConnectionLifecycle,
        GitLabManagementError,
    )

    database_url, schema, workspace = management_database

    async def exercise() -> None:
        engine = create_async_engine(
            database_url, connect_args={"options": f"-csearch_path={schema}"}, hide_parameters=True
        )
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        try:
            management = _management(sessions)
            installation = await _create(management, workspace)
            repository = (
                await management.bind_project(installation_id=installation, project_id=42)
            ).repository_id
            await _lifecycle_runs(sessions, repository, installation, installation)
            async with sessions() as session:
                before_runs = (
                    await session.execute(
                        text("SELECT row_to_json(r)::text FROM runs r ORDER BY id")
                    )
                ).all()
                before = (await session.execute(text("SELECT * FROM gitlab_connections"))).one()

            class RejectingProjects(ReplacementProjects):
                async def authenticate(self, *, origin: str, token: str) -> None:
                    if failure == "expected_generation":
                        raise AssertionError("expected generation must fail before network")
                    if failure == "authentication":
                        raise GitLabManagementError("invalid_token")
                    await super().authenticate(origin=origin, token=token)

                async def fetch_project(
                    self, *, origin: str, token: str, project_id: int
                ) -> RepositoryOnboardingInput:
                    raise GitLabManagementError("project_access_failed")

            lifecycle = _lifecycle(sessions, RejectingProjects())
            assert isinstance(lifecycle, GitLabConnectionLifecycle)
            with psycopg.connect(
                database_url.replace("postgresql+psycopg://", "postgresql://"), autocommit=True
            ) as listener:
                listener.execute("LISTEN run_updated")
                with pytest.raises(GitLabManagementError):
                    await lifecycle.rotate_token(
                        installation_id=installation,
                        token="replacement-token",
                        expected_generation=99 if failure == "expected_generation" else 1,
                    )
                assert list(listener.notifies(timeout=0.1)) == []
            async with sessions() as session:
                assert (
                    await session.execute(
                        text("SELECT row_to_json(r)::text FROM runs r ORDER BY id")
                    )
                ).all() == before_runs
                assert tuple(
                    (await session.execute(text("SELECT * FROM gitlab_connections"))).one()
                ) == tuple(before)
                assert await session.scalar(text("SELECT count(*) FROM rule_versions")) == 1
                assert (
                    await session.scalar(text("SELECT count(*) FROM gitlab_project_bindings")) == 1
                )
        finally:
            await engine.dispose()

    asyncio.run(exercise())


async def _lifecycle_runs(
    sessions: async_sessionmaker[AsyncSession],
    repository: UUID,
    installation: UUID,
    replacement: UUID,
) -> dict[str, UUID]:
    runs: dict[str, UUID] = {}
    async with sessions.begin() as session:
        rule = await session.scalar(
            text("SELECT id FROM rule_versions WHERE repository_id=:id AND is_active"),
            {"id": repository},
        )
        prompt = await session.scalar(text("SELECT id FROM prompt_versions WHERE is_active"))
        for number, state in enumerate(
            (
                "queued",
                "running",
                "publishing",
                "succeeded",
                "failed",
                "skipped",
                "cancelled",
                "replacement",
            ),
            1,
        ):
            change, run = uuid4(), uuid4()
            runs[state] = run
            await session.execute(
                text(
                    "INSERT INTO code_changes (id, repository_id, external_id, "
                    "external_number, title, "
                    "source_branch, target_branch, base_sha, head_sha, state, "
                    "web_url) VALUES (:id, :repository, "
                    ":number, :number, 'synthetic lifecycle test', 'feature', "
                    "'main', :base, :head, 'open', "
                    "'https://gitlab.com/group/project/-/merge_requests/1')"
                ),
                {
                    "id": change,
                    "repository": repository,
                    "number": number,
                    "base": "a" * 40,
                    "head": "b" * 40,
                },
            )
            await session.execute(
                text(
                    "INSERT INTO runs (id, code_change_id, "
                    "origin_provider_installation_id, base_sha, "
                    "head_sha, state, trigger, idempotency_key, engine, "
                    "rule_version_id, prompt_version_id, "
                    "attempt, available_at, lease_until, worker_id, finished_at, error_code, "
                    "cancellation_signal_requested_at) VALUES (:id, :change, "
                    ":origin, :base, :head, "
                    "CAST(:state AS run_state), 'manual', :key, 'fast', :rule, :prompt, :attempt, "
                    "clock_timestamp(), clock_timestamp()+interval '1 hour', 'live-worker', "
                    "CASE WHEN :terminal THEN '2026-01-01'::timestamptz ELSE NULL END, "
                    "CASE WHEN :terminal THEN 'historical-code' ELSE NULL END, "
                    "CASE WHEN :publishing THEN '2026-01-02'::timestamptz ELSE NULL END)"
                ),
                {
                    "id": run,
                    "change": change,
                    "origin": replacement if state == "replacement" else installation,
                    "base": "a" * 40,
                    "head": "b" * 40,
                    "state": "running" if state == "replacement" else state,
                    "key": uuid4().hex * 2,
                    "rule": rule,
                    "prompt": prompt,
                    "attempt": 0 if state == "queued" else 2,
                    "terminal": state in {"succeeded", "failed", "skipped", "cancelled"},
                    "publishing": state == "publishing",
                },
            )
    return runs


@pytest.mark.parametrize("scope", ["installation", "workspace"])
def test_keyless_revoke_is_idempotent_cancels_live_leases_and_preserves_other_tenants(
    management_database: tuple[str, str, UUID],
    scope: str,
) -> None:
    import json

    import psycopg

    from app.modules.integrations.gitlab.application.manage_connection import (
        GitLabConnectionLifecycle,
    )

    database_url, schema, workspace = management_database

    async def exercise() -> None:
        engine = create_async_engine(
            database_url, connect_args={"options": f"-csearch_path={schema}"}, hide_parameters=True
        )
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        try:
            other_workspace = uuid4()
            github = uuid4()
            async with sessions.begin() as session:
                await session.execute(
                    text(
                        "INSERT INTO workspaces (id, name, daily_budget_usd) VALUES "
                        "(:id, 'tenant B', 19)"
                    ),
                    {"id": other_workspace},
                )
                await session.execute(
                    text(
                        "INSERT INTO provider_installations (id, workspace_id, "
                        "provider, external_id, metadata) VALUES (:id, :workspace, "
                        "'github', 123456, '{}')"
                    ),
                    {"id": github, "workspace": workspace},
                )
            management = _management(sessions)
            first = await _create(management, workspace)
            sibling = await _create(management, workspace)
            replacement = await _create(management, workspace)
            first_repo = await management.bind_project(installation_id=first, project_id=42)
            sibling_repo = await management.bind_project(installation_id=sibling, project_id=99)
            other = _management(sessions, Projects("https://gitlab.example.com"))
            other_id = await _create(other, other_workspace, origin="https://gitlab.example.com")
            other_repo = await other.bind_project(installation_id=other_id, project_id=42)
            # Replacement-origin run must survive installation-only revocation even on the old repo.
            runs = await _lifecycle_runs(sessions, first_repo.repository_id, first, replacement)
            async with sessions.begin() as session:
                await session.execute(
                    text("UPDATE repositories SET enabled=false, max_comments=3 WHERE id=:id"),
                    {"id": first_repo.repository_id},
                )
                await session.execute(
                    text(
                        "UPDATE gitlab_connections SET intake_enabled=true WHERE "
                        "provider_installation_id=:id"
                    ),
                    {"id": first},
                )
                before_terminal = (
                    await session.execute(
                        text(
                            "SELECT id, state, attempt, finished_at, error_code, "
                            "lease_until, worker_id FROM runs WHERE state IN "
                            "('succeeded','failed','skipped','cancelled') ORDER BY id"
                        )
                    )
                ).all()
                other_credential = (
                    await session.execute(
                        text(
                            "SELECT token_ciphertext, credential_key_id, "
                            "credential_generation FROM gitlab_connections WHERE "
                            "provider_installation_id=:id"
                        ),
                        {"id": other_id},
                    )
                ).one()
                original_signal = await session.scalar(
                    text("SELECT cancellation_signal_requested_at FROM runs WHERE id=:id"),
                    {"id": runs["publishing"]},
                )
                github_before = (
                    await session.execute(
                        text("SELECT * FROM provider_installations WHERE id=:id"), {"id": github}
                    )
                ).one()
            lifecycle = GitLabConnectionLifecycle(
                uow_factory=lambda: SqlAlchemyGitLabManagementUnitOfWork(sessions)
            )
            with psycopg.connect(
                database_url.replace("postgresql+psycopg://", "postgresql://"), autocommit=True
            ) as listener:
                listener.execute("LISTEN run_updated")
                if scope == "installation":
                    result = await lifecycle.revoke_installation(installation_id=first)
                    assert result.generation == 2
                else:
                    results = await lifecycle.revoke_workspace(workspace_id=workspace)
                    assert len(results) == 3
                    assert all(result.revoked and result.generation == 2 for result in results)
                expected_ids = {runs["queued"], runs["running"], runs["publishing"]}
                if scope == "workspace":
                    expected_ids.add(runs["replacement"])
                notifications = [
                    json.loads(note.payload)
                    for note in listener.notifies(timeout=0.3, stop_after=len(expected_ids))
                ]
                assert {UUID(note["run_id"]) for note in notifications} == expected_ids
                assert all(
                    set(note) == {"run_id", "workspace_id", "status"}
                    and note["workspace_id"] == str(workspace)
                    and note["status"] == "cancelled"
                    for note in notifications
                )
                async with sessions() as session:
                    timestamp = await session.scalar(
                        text(
                            "SELECT revoked_at FROM gitlab_connections WHERE "
                            "provider_installation_id=:id"
                        ),
                        {"id": first},
                    )
                if scope == "installation":
                    assert await lifecycle.revoke_installation(installation_id=first) == result
                else:
                    assert await lifecycle.revoke_workspace(workspace_id=workspace) == results
                assert list(listener.notifies(timeout=0.1)) == []
            from datetime import UTC, datetime

            from app.modules.reviews.infrastructure.run_lifecycle_store import (
                SqlAlchemyRunLifecycleStore,
            )

            async with sessions.begin() as session:
                assert (
                    await SqlAlchemyRunLifecycleStore(session).finish(
                        runs["running"],
                        from_state="running",
                        worker_id="live-worker",
                        state="succeeded",
                        error_code=None,
                        error_message=None,
                        now=datetime.now(UTC),
                    )
                    is False
                )
            inspection = await lifecycle.inspect(installation_id=first)
            assert inspection.connection.revoked is True
            assert inspection.connection.intake_enabled is False
            assert inspection.repositories[0].available is False
            assert inspection.repositories[0].enabled is False
            assert inspection.repositories[0].rules_ready is True
            assert inspection.prompt_ready is True
            assert "test-key" not in repr(inspection)
            async with sessions() as session:
                row = (
                    await session.execute(
                        text(
                            "SELECT revoked_at, credential_generation, token_ciphertext, "
                            "credential_key_id, intake_enabled FROM gitlab_connections "
                            "WHERE provider_installation_id=:id"
                        ),
                        {"id": first},
                    )
                ).one()
                assert tuple(row) == (timestamp, 2, None, None, False)
                assert (
                    await session.scalar(
                        text(
                            "SELECT deleted_at IS NOT NULL FROM provider_installations WHERE id=:id"
                        ),
                        {"id": first},
                    )
                    is True
                )
                assert (
                    await session.execute(
                        text(
                            "SELECT id, state, attempt, finished_at, error_code, "
                            "lease_until, worker_id FROM runs WHERE state IN "
                            "('succeeded','failed','skipped','cancelled') AND id NOT IN "
                            "(:queued, :running, :publishing, :replacement) ORDER BY id"
                        ),
                        runs,
                    )
                ).all() == before_terminal
                active_rows = (
                    await session.execute(
                        text(
                            "SELECT id, state, cancel_requested, attempt, lease_until, "
                            "worker_id, cancellation_signal_requested_at FROM runs WHERE "
                            "id IN (:queued,:running,:publishing)"
                        ),
                        runs,
                    )
                ).all()
                assert len(active_rows) == 3
                for row in active_rows:
                    assert tuple(row)[1:6] == (
                        "cancelled",
                        True,
                        0 if row[0] == runs["queued"] else 2,
                        None,
                        None,
                    )
                    assert (row[6] is None) == (row[0] == runs["queued"])
                assert (
                    await session.scalar(
                        text("SELECT cancellation_signal_requested_at FROM runs WHERE id=:id"),
                        {"id": runs["publishing"]},
                    )
                    == original_signal
                )
                assert await session.scalar(
                    text("SELECT state FROM runs WHERE id=:id"), {"id": runs["replacement"]}
                ) == ("running" if scope == "installation" else "cancelled")
                assert tuple(
                    (
                        await session.execute(
                            text(
                                "SELECT token_ciphertext, credential_key_id, "
                                "credential_generation FROM gitlab_connections WHERE "
                                "provider_installation_id=:id"
                            ),
                            {"id": other_id},
                        )
                    ).one()
                ) == tuple(other_credential)
                assert (
                    await session.execute(
                        text("SELECT * FROM provider_installations WHERE id=:id"), {"id": github}
                    )
                ).one() == github_before
                assert (
                    await session.scalar(
                        text("SELECT max_comments FROM repositories WHERE id=:id"),
                        {"id": first_repo.repository_id},
                    )
                    == 3
                )
                assert await session.scalar(text("SELECT count(*) FROM rule_versions")) == 3
                assert await session.scalar(text("SELECT count(*) FROM code_changes")) == 8
                assert (
                    await session.scalar(
                        text("SELECT daily_budget_usd FROM workspaces WHERE id=:id"),
                        {"id": workspace},
                    )
                    == 7
                )
                assert await session.scalar(
                    text("SELECT installation_available FROM repositories WHERE id=:id"),
                    {"id": sibling_repo.repository_id},
                ) is (scope == "installation")
                assert (
                    await session.scalar(
                        text("SELECT installation_available FROM repositories WHERE id=:id"),
                        {"id": other_repo.repository_id},
                    )
                    is True
                )
            # Tenant B's unchanged real credential still decrypts and binds.
            await other.bind_project(installation_id=other_id, project_id=43)
            # The serialized workspace revoke is not a permanent enrollment ban.
            later = await _create(management, workspace)
            rebound = await management.bind_project(installation_id=later, project_id=42)
            assert rebound.repository_id != first_repo.repository_id
            assert (await lifecycle.inspect(installation_id=later)).connection.revoked is False
        finally:
            await engine.dispose()

    asyncio.run(exercise())


def test_concurrent_rotations_have_one_winner_and_one_generation_increment(
    management_database: tuple[str, str, UUID],
) -> None:
    import json

    import psycopg

    from app.modules.integrations.gitlab.application.manage_connection import (
        ConnectionResult,
        GitLabConnectionLifecycle,
        GitLabManagementError,
    )

    database_url, schema, workspace = management_database

    async def exercise() -> None:
        engine = create_async_engine(
            database_url, connect_args={"options": f"-csearch_path={schema}"}, hide_parameters=True
        )
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        ready = asyncio.Event()
        arrived = 0

        class ConcurrentProjects(ReplacementProjects):
            async def fetch_project(
                self, *, origin: str, token: str, project_id: int
            ) -> RepositoryOnboardingInput:
                nonlocal arrived
                arrived += 1
                if arrived == 2:
                    ready.set()
                await asyncio.wait_for(ready.wait(), timeout=10)
                return await super().fetch_project(
                    origin=origin, token=token, project_id=project_id
                )

        try:
            management = _management(sessions)
            installation = await _create(management, workspace)
            repository = (
                await management.bind_project(installation_id=installation, project_id=42)
            ).repository_id
            runs = await _lifecycle_runs(sessions, repository, installation, installation)
            lifecycle = _lifecycle(sessions, ConcurrentProjects())
            assert isinstance(lifecycle, GitLabConnectionLifecycle)
            with psycopg.connect(
                database_url.replace("postgresql+psycopg://", "postgresql://"), autocommit=True
            ) as listener:
                listener.execute("LISTEN run_updated")
                outcomes = await asyncio.wait_for(
                    asyncio.gather(
                        lifecycle.rotate_token(
                            installation_id=installation, token="replacement-token"
                        ),
                        lifecycle.rotate_token(
                            installation_id=installation, token="replacement-token"
                        ),
                        return_exceptions=True,
                    ),
                    timeout=15,
                )
                notices = [json.loads(note.payload) for note in listener.notifies(timeout=0.1)]
            assert len(notices) == 4
            assert {UUID(note["run_id"]) for note in notices} == {
                runs[name] for name in ("queued", "running", "publishing", "replacement")
            }
            assert all(
                note
                == {"run_id": note["run_id"], "workspace_id": str(workspace), "status": "cancelled"}
                for note in notices
            )
            async with sessions() as session:
                assert (
                    await session.scalar(
                        text("SELECT count(*) FROM runs WHERE error_code='credential_rotated'")
                    )
                    == 4
                )
            assert sum(isinstance(item, ConnectionResult) for item in outcomes) == 1
            failures = [item for item in outcomes if isinstance(item, GitLabManagementError)]
            assert len(failures) == 1
            assert failures[0].code == "connection_changed"
            assert (
                await lifecycle.inspect(installation_id=installation)
            ).connection.generation == 2
        finally:
            await engine.dispose()

    asyncio.run(exercise())


def test_revocation_rollback_has_no_partial_retirement_or_notification(
    management_database: tuple[str, str, UUID],
) -> None:
    import psycopg
    from sqlalchemy.exc import IntegrityError

    from app.modules.integrations.gitlab.application.manage_connection import (
        GitLabConnectionLifecycle,
    )

    database_url, schema, workspace = management_database

    async def exercise() -> None:
        engine = create_async_engine(
            database_url, connect_args={"options": f"-csearch_path={schema}"}, hide_parameters=True
        )
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        try:
            management = _management(sessions)
            installation = await _create(management, workspace)
            replacement = await _create(management, workspace)
            repository = (
                await management.bind_project(installation_id=installation, project_id=42)
            ).repository_id
            await _lifecycle_runs(sessions, repository, installation, replacement)
            async with sessions.begin() as session:
                await session.execute(
                    text(
                        "ALTER TABLE runs ADD CONSTRAINT ck_test_no_cancel CHECK "
                        "(state <> 'cancelled') NOT VALID"
                    )
                )
            lifecycle = GitLabConnectionLifecycle(
                uow_factory=lambda: SqlAlchemyGitLabManagementUnitOfWork(sessions)
            )
            with psycopg.connect(
                database_url.replace("postgresql+psycopg://", "postgresql://"), autocommit=True
            ) as listener:
                listener.execute("LISTEN run_updated")
                with pytest.raises(IntegrityError):
                    await lifecycle.revoke_installation(installation_id=installation)
                assert list(listener.notifies(timeout=0.1)) == []
            inspection = await lifecycle.inspect(installation_id=installation)
            assert inspection.connection.revoked is False
            assert inspection.connection.generation == 1
            assert inspection.repositories[0].available is True
            assert inspection.repositories[0].rules_ready is True
            async with sessions() as session:
                assert (
                    await session.scalar(text("SELECT count(*) FROM gitlab_project_bindings")) == 1
                )
                assert (
                    await session.scalar(
                        text("SELECT deleted_at FROM provider_installations WHERE id=:id"),
                        {"id": installation},
                    )
                    is None
                )
                assert (
                    await session.scalar(text("SELECT count(*) FROM runs WHERE state='running'"))
                    == 2
                )
            await management.bind_project(installation_id=installation, project_id=43)
        finally:
            await engine.dispose()

    asyncio.run(exercise())


def test_rotation_provider_calls_have_no_open_sql_transaction(
    management_database: tuple[str, str, UUID],
) -> None:
    from sqlalchemy import Connection, event

    from app.modules.integrations.gitlab.application.manage_connection import (
        GitLabConnectionLifecycle,
    )

    database_url, schema, workspace = management_database

    async def exercise() -> None:
        engine = create_async_engine(
            database_url, connect_args={"options": f"-csearch_path={schema}"}, hide_parameters=True
        )
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        transactions: set[Connection] = set()

        def began(connection: Connection) -> None:
            transactions.add(connection)

        def ended(connection: Connection) -> None:
            transactions.remove(connection)

        event.listen(engine.sync_engine, "begin", began)
        event.listen(engine.sync_engine, "commit", ended)
        event.listen(engine.sync_engine, "rollback", ended)

        class CheckedProjects(ReplacementProjects):
            async def authenticate(self, *, origin: str, token: str) -> None:
                assert not transactions
                await super().authenticate(origin=origin, token=token)

        try:
            management = _management(sessions)
            installation = await _create(management, workspace)
            await management.bind_project(installation_id=installation, project_id=42)
            lifecycle = _lifecycle(sessions, CheckedProjects())
            assert isinstance(lifecycle, GitLabConnectionLifecycle)
            await lifecycle.rotate_token(installation_id=installation, token="replacement-token")
            assert not transactions
        finally:
            await engine.dispose()

    asyncio.run(exercise())


async def _attempted_queued_run(
    sessions: async_sessionmaker[AsyncSession],
    source: UUID,
) -> UUID:
    change, run = uuid4(), uuid4()
    async with sessions.begin() as session:
        await session.execute(
            text(
                "INSERT INTO code_changes (id, repository_id, external_id, external_number, title, "
                "source_branch, target_branch, base_sha, head_sha, state, web_url) "
                "SELECT :id, repository_id, 99, 99, title, source_branch, target_branch, base_sha, "
                "head_sha, state, web_url FROM code_changes WHERE id="
                "(SELECT code_change_id FROM runs WHERE id=:source)"
            ),
            {"id": change, "source": source},
        )
        await session.execute(
            text(
                "INSERT INTO runs (id, code_change_id, origin_provider_installation_id, base_sha, "
                "head_sha, state, trigger, idempotency_key, engine, "
                "rule_version_id, prompt_version_id, "
                "attempt, available_at) SELECT :id, :change, "
                "origin_provider_installation_id, base_sha, "
                "head_sha, 'queued', trigger, :key, engine, rule_version_id, prompt_version_id, 1, "
                "available_at FROM runs WHERE id=:source"
            ),
            {"id": run, "change": change, "key": uuid4().hex * 2, "source": source},
        )
    return run


def test_rotation_retires_exact_origin_work_atomically_and_keeps_connection_active(
    management_database: tuple[str, str, UUID],
) -> None:
    import json
    from datetime import UTC, datetime

    import psycopg

    from app.modules.integrations.gitlab.application.manage_connection import (
        GitLabConnectionLifecycle,
    )

    database_url, schema, workspace = management_database

    async def exercise() -> None:
        engine = create_async_engine(
            database_url, connect_args={"options": f"-csearch_path={schema}"}, hide_parameters=True
        )
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        try:
            other_workspace, github = uuid4(), uuid4()
            async with sessions.begin() as session:
                await session.execute(
                    text(
                        "INSERT INTO workspaces (id, name, daily_budget_usd) VALUES "
                        "(:id, 'tenant B', 19)"
                    ),
                    {"id": other_workspace},
                )
                await session.execute(
                    text(
                        "INSERT INTO provider_installations (id, workspace_id, "
                        "provider, external_id, "
                        "metadata) VALUES (:id, :workspace, 'github', 123456, '{}')"
                    ),
                    {"id": github, "workspace": workspace},
                )
            management = _management(sessions)
            installation = await _create(management, workspace)
            repository = (
                await management.bind_project(installation_id=installation, project_id=42)
            ).repository_id
            other = _management(sessions, Projects("https://gitlab.example.com"))
            other_id = await _create(other, other_workspace, origin="https://gitlab.example.com")
            other_repository = (
                await other.bind_project(installation_id=other_id, project_id=42)
            ).repository_id
            runs = await _lifecycle_runs(sessions, repository, installation, github)
            await _lifecycle_runs(sessions, other_repository, other_id, other_id)
            runs["queued_retry"] = await _attempted_queued_run(sessions, runs["queued"])
            async with sessions.begin() as session:
                await session.execute(
                    text("UPDATE repositories SET enabled=false, max_comments=3 WHERE id=:id"),
                    {"id": repository},
                )
                await session.execute(
                    text(
                        "UPDATE gitlab_connections SET intake_enabled=true WHERE "
                        "provider_installation_id=:id"
                    ),
                    {"id": installation},
                )
                terminal = (
                    await session.execute(
                        text(
                            "SELECT row_to_json(r)::text FROM runs r WHERE state IN "
                            "('succeeded','failed','skipped','cancelled') ORDER BY id"
                        )
                    )
                ).all()
                foreign = (
                    await session.execute(
                        text(
                            "SELECT row_to_json(r)::text FROM runs r WHERE "
                            "origin_provider_installation_id<>:id ORDER BY id"
                        ),
                        {"id": installation},
                    )
                ).all()
                parents = (
                    await session.execute(
                        text(
                            "SELECT row_to_json(p)::text FROM provider_installations p ORDER BY id"
                        )
                    )
                ).all()
                repositories = (
                    await session.execute(
                        text("SELECT row_to_json(r)::text FROM repositories r ORDER BY id")
                    )
                ).all()
                bindings = (
                    await session.execute(
                        text(
                            "SELECT row_to_json(b)::text FROM gitlab_project_bindings b "
                            "ORDER BY repository_id"
                        )
                    )
                ).all()
                rules = (
                    await session.execute(
                        text("SELECT row_to_json(r)::text FROM rule_versions r ORDER BY id")
                    )
                ).all()
                prompts = (
                    await session.execute(
                        text("SELECT row_to_json(p)::text FROM prompt_versions p ORDER BY id")
                    )
                ).all()
                digest = await session.scalar(
                    text(
                        "SELECT webhook_secret_digest FROM gitlab_connections WHERE "
                        "provider_installation_id=:id"
                    ),
                    {"id": installation},
                )
                publishing_signal = await session.scalar(
                    text("SELECT cancellation_signal_requested_at FROM runs WHERE id=:id"),
                    {"id": runs["publishing"]},
                )
            lifecycle = _lifecycle(sessions)
            assert isinstance(lifecycle, GitLabConnectionLifecycle)
            before = datetime.now(UTC)
            with psycopg.connect(
                database_url.replace("postgresql+psycopg://", "postgresql://"), autocommit=True
            ) as listener:
                listener.execute("LISTEN run_updated")
                result = await lifecycle.rotate_token(
                    installation_id=installation, token="replacement-token", expected_generation=1
                )
                after = datetime.now(UTC)
                notices = [json.loads(note.payload) for note in listener.notifies(timeout=0.1)]
            affected = {runs[name] for name in ("queued", "queued_retry", "running", "publishing")}
            async with sessions() as session:
                rows = (
                    await session.execute(
                        text(
                            "SELECT id, state, error_code, cancel_requested, attempt, "
                            "lease_until, worker_id, "
                            "finished_at, cancellation_signal_requested_at FROM runs "
                            "WHERE origin_provider_installation_id=:id AND id IN "
                            "(:queued,:queued_retry,:running,:publishing)"
                        ),
                        {"id": installation, **runs},
                    )
                ).all()
                assert len(rows) == 4
                for row in rows:
                    assert tuple(row)[1:7] == (
                        "cancelled",
                        "credential_rotated",
                        True,
                        0
                        if row[0] == runs["queued"]
                        else 1
                        if row[0] == runs["queued_retry"]
                        else 2,
                        None,
                        None,
                    )
                    assert before <= row[7] <= after
                    assert (row[8] is None) == (row[0] == runs["queued"])
                assert (
                    await session.scalar(
                        text("SELECT cancellation_signal_requested_at FROM runs WHERE id=:id"),
                        {"id": runs["publishing"]},
                    )
                    == publishing_signal
                )
                assert (
                    await session.execute(
                        text(
                            "SELECT row_to_json(r)::text FROM runs r WHERE state IN "
                            "('succeeded','failed','skipped','cancelled') AND id NOT IN "
                            "(:queued,:queued_retry,:running,:publishing) ORDER BY id"
                        ),
                        runs,
                    )
                ).all() == terminal
                assert (
                    await session.execute(
                        text(
                            "SELECT row_to_json(r)::text FROM runs r WHERE "
                            "origin_provider_installation_id<>:id ORDER BY id"
                        ),
                        {"id": installation},
                    )
                ).all() == foreign
                assert (
                    await session.execute(
                        text(
                            "SELECT row_to_json(p)::text FROM provider_installations p ORDER BY id"
                        )
                    )
                ).all() == parents
                assert (
                    await session.execute(
                        text("SELECT row_to_json(r)::text FROM repositories r ORDER BY id")
                    )
                ).all() == repositories
                assert (
                    await session.execute(
                        text(
                            "SELECT row_to_json(b)::text FROM gitlab_project_bindings b "
                            "ORDER BY repository_id"
                        )
                    )
                ).all() == bindings
                assert (
                    await session.execute(
                        text("SELECT row_to_json(r)::text FROM rule_versions r ORDER BY id")
                    )
                ).all() == rules
                assert (
                    await session.execute(
                        text("SELECT row_to_json(p)::text FROM prompt_versions p ORDER BY id")
                    )
                ).all() == prompts
                connection = (
                    await session.execute(
                        text(
                            "SELECT credential_generation, credential_key_id, revoked_at, "
                            "intake_enabled, "
                            "webhook_secret_digest, token_ciphertext IS NOT NULL FROM "
                            "gitlab_connections "
                            "WHERE provider_installation_id=:id"
                        ),
                        {"id": installation},
                    )
                ).one()
                assert tuple(connection) == (2, "replacement-key", None, True, digest, True)
            assert result.generation == 2
            assert result.revoked is False
            assert result.intake_enabled is True
            assert len(notices) == 4
            assert {UUID(note["run_id"]) for note in notices} == affected
            assert all(
                note
                == {"run_id": note["run_id"], "workspace_id": str(workspace), "status": "cancelled"}
                for note in notices
            )
        finally:
            await engine.dispose()

    asyncio.run(exercise())


@pytest.mark.parametrize("operation", ["rotate", "revoke"])
def test_historical_origin_retirement_notifies_origin_workspace_after_repository_replacement(
    management_database: tuple[str, str, UUID],
    operation: str,
) -> None:
    import json

    import psycopg

    from app.modules.integrations.gitlab.application.manage_connection import (
        GitLabConnectionLifecycle,
    )

    database_url, schema, workspace = management_database

    async def exercise() -> None:
        engine = create_async_engine(
            database_url, connect_args={"options": f"-csearch_path={schema}"}, hide_parameters=True
        )
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        try:
            other_workspace = uuid4()
            async with sessions.begin() as session:
                await session.execute(
                    text(
                        "INSERT INTO workspaces (id, name, daily_budget_usd) VALUES "
                        "(:id, 'replacement', 19)"
                    ),
                    {"id": other_workspace},
                )
            management = _management(sessions)
            installation = await _create(management, workspace)
            repository = (
                await management.bind_project(installation_id=installation, project_id=42)
            ).repository_id
            other = _management(sessions, Projects("https://gitlab.example.com"))
            replacement = await _create(other, other_workspace, origin="https://gitlab.example.com")
            runs = await _lifecycle_runs(sessions, repository, installation, replacement)
            async with sessions.begin() as session:
                # Historical Run.origin stays immutable; only current repository ownership changes.
                await session.execute(
                    text("DELETE FROM gitlab_project_bindings WHERE repository_id=:id"),
                    {"id": repository},
                )
                await session.execute(
                    text(
                        "UPDATE repositories SET "
                        "provider_installation_id=:replacement WHERE id=:repository"
                    ),
                    {"replacement": replacement, "repository": repository},
                )
                await session.execute(
                    text(
                        "INSERT INTO gitlab_project_bindings (repository_id, "
                        "provider_installation_id, "
                        "instance_origin, project_external_id) VALUES (:repository, :replacement, "
                        "'https://gitlab.example.com', 42)"
                    ),
                    {"repository": repository, "replacement": replacement},
                )
                foreign = await session.scalar(
                    text("SELECT row_to_json(r)::text FROM runs r WHERE id=:id"),
                    {"id": runs["replacement"]},
                )
                replacement_before = await session.scalar(
                    text(
                        "SELECT row_to_json(c)::text FROM gitlab_connections c WHERE "
                        "provider_installation_id=:id"
                    ),
                    {"id": replacement},
                )
                repository_before = await session.scalar(
                    text("SELECT row_to_json(r)::text FROM repositories r WHERE id=:id"),
                    {"id": repository},
                )
            lifecycle = _lifecycle(sessions)
            assert isinstance(lifecycle, GitLabConnectionLifecycle)
            with psycopg.connect(
                database_url.replace("postgresql+psycopg://", "postgresql://"), autocommit=True
            ) as listener:
                listener.execute("LISTEN run_updated")
                if operation == "rotate":
                    await lifecycle.rotate_token(
                        installation_id=installation, token="replacement-token"
                    )
                else:
                    await lifecycle.revoke_installation(installation_id=installation)
                notices = [json.loads(note.payload) for note in listener.notifies(timeout=0.1)]
            affected = {runs[name] for name in ("queued", "running", "publishing")}
            assert len(notices) == 3
            assert {UUID(note["run_id"]) for note in notices} == affected
            assert all(
                note
                == {"run_id": note["run_id"], "workspace_id": str(workspace), "status": "cancelled"}
                for note in notices
            )
            async with sessions() as session:
                assert (
                    await session.scalar(
                        text("SELECT row_to_json(r)::text FROM runs r WHERE id=:id"),
                        {"id": runs["replacement"]},
                    )
                    == foreign
                )
                assert (
                    await session.scalar(
                        text(
                            "SELECT row_to_json(c)::text FROM gitlab_connections c WHERE "
                            "provider_installation_id=:id"
                        ),
                        {"id": replacement},
                    )
                    == replacement_before
                )
                assert (
                    await session.scalar(
                        text("SELECT row_to_json(r)::text FROM repositories r WHERE id=:id"),
                        {"id": repository},
                    )
                    == repository_before
                )
        finally:
            await engine.dispose()

    asyncio.run(exercise())


def test_rotation_commit_failure_rolls_back_credentials_runs_and_notifications(
    management_database: tuple[str, str, UUID],
) -> None:
    import psycopg
    from sqlalchemy.exc import IntegrityError

    from app.modules.integrations.gitlab.application.manage_connection import (
        GitLabConnectionLifecycle,
    )

    database_url, schema, workspace = management_database

    async def exercise() -> None:
        engine = create_async_engine(
            database_url, connect_args={"options": f"-csearch_path={schema}"}, hide_parameters=True
        )
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        try:
            management = _management(sessions)
            installation = await _create(management, workspace)
            repository = (
                await management.bind_project(installation_id=installation, project_id=42)
            ).repository_id
            await _lifecycle_runs(sessions, repository, installation, installation)
            async with sessions.begin() as session:
                # Deferred trigger rejects COMMIT after run writes and pg_notify have executed.
                await session.execute(
                    text(
                        "CREATE FUNCTION reject_rotation_retirement() RETURNS trigger "
                        "LANGUAGE plpgsql "
                        "AS $$ BEGIN RAISE EXCEPTION 'synthetic deferred retirement failure' "
                        "USING ERRCODE='23514'; RETURN NEW; END $$"
                    )
                )
                await session.execute(
                    text(
                        "CREATE CONSTRAINT TRIGGER reject_rotation_retirement AFTER UPDATE ON runs "
                        "DEFERRABLE INITIALLY DEFERRED FOR EACH ROW "
                        "WHEN (NEW.error_code = 'credential_rotated') "
                        "EXECUTE FUNCTION reject_rotation_retirement()"
                    )
                )
                before_connection = await session.scalar(
                    text("SELECT row_to_json(c)::text FROM gitlab_connections c")
                )
                before_runs = (
                    await session.execute(
                        text("SELECT row_to_json(r)::text FROM runs r ORDER BY id")
                    )
                ).all()
                before_bindings = (
                    await session.execute(
                        text("SELECT row_to_json(b)::text FROM gitlab_project_bindings b")
                    )
                ).all()
                before_repository = await session.scalar(
                    text("SELECT row_to_json(r)::text FROM repositories r")
                )
            lifecycle = _lifecycle(sessions)
            assert isinstance(lifecycle, GitLabConnectionLifecycle)
            with psycopg.connect(
                database_url.replace("postgresql+psycopg://", "postgresql://"), autocommit=True
            ) as listener:
                listener.execute("LISTEN run_updated")
                with pytest.raises(IntegrityError):
                    await lifecycle.rotate_token(
                        installation_id=installation,
                        token="replacement-token",
                        expected_generation=1,
                    )
                assert list(listener.notifies(timeout=0.1)) == []
            async with sessions() as session:
                assert (
                    await session.scalar(
                        text("SELECT row_to_json(c)::text FROM gitlab_connections c")
                    )
                    == before_connection
                )
                assert (
                    await session.execute(
                        text("SELECT row_to_json(r)::text FROM runs r ORDER BY id")
                    )
                ).all() == before_runs
                assert (
                    await session.execute(
                        text("SELECT row_to_json(b)::text FROM gitlab_project_bindings b")
                    )
                ).all() == before_bindings
                assert (
                    await session.scalar(text("SELECT row_to_json(r)::text FROM repositories r"))
                    == before_repository
                )
            await management.bind_project(installation_id=installation, project_id=43)
        finally:
            await engine.dispose()

    asyncio.run(exercise())


@dataclass
class ActivationRig:
    sessions: async_sessionmaker[AsyncSession]
    management: ManageGitLabConnection
    lifecycle: GitLabConnectionLifecycle
    requests: list[tuple[str, str]]
    before_project: Callable[[], Awaitable[None]] | None = None
    failure: str | None = None

    async def sql(self, query: str, **params: object) -> list[tuple[object, ...]]:
        async with self.sessions() as session:
            result = await session.execute(text(query), params)
            rows = (
                [tuple(row) for row in result]
                if isinstance(result, CursorResult) and result.returns_rows
                else []
            )
            await session.commit()
            return rows

    async def state(self) -> dict[str, list[tuple[object, ...]]]:
        return {
            table: await self.sql(f"SELECT to_jsonb(t) FROM {table} t ORDER BY to_jsonb(t)::text")
            for table in (
                "provider_installations",
                "gitlab_connections",
                "gitlab_project_bindings",
                "repositories",
                "rule_versions",
                "prompt_versions",
                "workspaces",
                "runs",
            )
        }


@asynccontextmanager
async def activation_rig(database: tuple[str, str, UUID]) -> AsyncIterator[ActivationRig]:
    database_url, schema, _ = database
    engine = create_async_engine(
        database_url,
        connect_args={
            "options": f"-csearch_path={schema}",
            "application_name": "dmc169-activation",
        },
        pool_size=1,
        max_overflow=0,
        pool_timeout=1,
        hide_parameters=True,
    )
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    codec = CredentialCipherAdapter(
        CredentialCipher(active_key_id="test-key", keys={"test-key": b"k" * 32})
    )
    policy = GitLabOriginPolicy(frozenset({_ORIGIN, "https://gitlab.example.com"}))
    requests: list[tuple[str, str]] = []

    async def response(request: httpx.Request) -> httpx.Response:
        # A single-slot pool proves the service released its DB lease for EVERY GET.
        async with engine.connect() as connection:
            assert (
                await connection.scalar(
                    text(
                        "SELECT count(*) FROM pg_stat_activity WHERE "
                        "application_name='dmc169-activation' AND pid<>pg_backend_pid() "
                        "AND backend_type='client backend' AND xact_start IS NOT NULL"
                    )
                )
                == 0
            )
        assert request.headers["PRIVATE-TOKEN"] == _TOKEN
        requests.append((str(request.url.host), request.url.path))
        if rig.failure == "tls":
            raise httpx.ConnectError("TLS_SECRET_CANARY", request=request)
        if rig.failure == "pat":
            return httpx.Response(401, json={"token": "PAT_SECRET_CANARY"})
        path = request.url.path
        if path == "/api/v4/user":
            return httpx.Response(200, json={"id": 1, "state": "active"})
        if path.endswith("/languages"):
            return httpx.Response(200, json={"Python": 100})
        if rig.before_project is not None:
            action, rig.before_project = rig.before_project, None
            await action()
        project_id = int(path.rsplit("/", 1)[1])
        return httpx.Response(
            200,
            json={
                "id": project_id,
                "path_with_namespace": f"group/project{project_id}",
                "default_branch": "main",
                "web_url": f"{request.url.scheme}://{request.url.host}/group/project{project_id}",
                "archived": False,
                "empty_repo": False,
            },
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(response), trust_env=False
    ) as client:
        projects = GitLabProjectDetails(client, origin_policy=policy)
        management = ManageGitLabConnection(
            uow_factory=lambda: SqlAlchemyGitLabManagementUnitOfWork(sessions),
            codec=codec,
            projects=projects,
            origin_policy=policy,
            rule_sets=load_default_rule_sets(Path("review/rules")),
        )
        lifecycle = GitLabConnectionLifecycle(
            uow_factory=lambda: SqlAlchemyGitLabManagementUnitOfWork(sessions),
            codec=codec,
            projects=projects,
            origin_policy=policy,
        )
        rig = ActivationRig(sessions, management, lifecycle, requests)
        try:
            yield rig
        finally:
            await engine.dispose()


def test_public_activation_only_enables_selected_connection_and_repeat_reauthenticates(
    management_database: tuple[str, str, UUID],
) -> None:
    async def exercise() -> None:
        async with activation_rig(management_database) as rig:
            first = await _create(rig.management, management_database[2])
            ready = await rig.management.bind_project(installation_id=first, project_id=42)
            paused = await rig.management.bind_project(installation_id=first, project_id=43)
            other_workspace = uuid4()
            await rig.sql(
                "INSERT INTO workspaces(id,name,daily_budget_usd) VALUES (:id,'other',0)",
                id=other_workspace,
            )
            other = await _create(
                rig.management, other_workspace, origin="https://gitlab.example.com"
            )
            await rig.management.bind_project(installation_id=other, project_id=42)
            await rig.sql(
                "UPDATE repositories SET enabled=false WHERE id=:id", id=paused.repository_id
            )
            await rig.sql("UPDATE workspaces SET daily_budget_usd=0")
            before = await rig.state()
            rig.requests.clear()
            with psycopg.connect(
                management_database[0].replace("postgresql+psycopg://", "postgresql://"),
                autocommit=True,
            ) as listener:
                listener.execute("LISTEN run_updated")
                activated = await rig.lifecycle.activate(
                    installation_id=first, expected_generation=1
                )
                assert activated.intake_enabled is True and activated.generation == 1
                after = await rig.state()
                assert {
                    key: value for key, value in after.items() if key != "gitlab_connections"
                } == {key: value for key, value in before.items() if key != "gitlab_connections"}
                assert await rig.sql(
                    "SELECT intake_enabled,credential_generation FROM gitlab_connections "
                    "WHERE provider_installation_id=:id",
                    id=other,
                ) == [(False, 1)]
                assert await rig.sql(
                    "SELECT enabled FROM repositories WHERE id=:id", id=paused.repository_id
                ) == [(False,)]
                assert await rig.sql(
                    "SELECT enabled FROM repositories WHERE id=:id", id=ready.repository_id
                ) == [(True,)]
                changed_before = [row[0] for row in before["gitlab_connections"]]
                changed_after = [row[0] for row in after["gitlab_connections"]]
                assert all(isinstance(row, dict) for row in changed_before + changed_after)

                def stable(rows: list[object]) -> list[object]:
                    return sorted(
                        (
                            {
                                key: value
                                for key, value in row.items()
                                if key not in ("intake_enabled", "updated_at")
                            }
                            for row in rows
                            if isinstance(row, dict)
                        ),
                        key=lambda row: str(row),
                    )

                assert stable(changed_after) == stable(changed_before)
                assert [path for _, path in rig.requests if path.endswith("/languages")] == [
                    "/api/v4/projects/42/languages",
                    "/api/v4/projects/43/languages",
                ]
                rig.requests.clear()
                assert (
                    await rig.lifecycle.activate(installation_id=first, expected_generation=1)
                    == activated
                )
                assert await rig.state() == after
                assert [path for _, path in rig.requests if path.endswith("/languages")] == [
                    "/api/v4/projects/42/languages",
                    "/api/v4/projects/43/languages",
                ]
                assert list(listener.notifies(timeout=0.05)) == []

    asyncio.run(exercise())


@pytest.mark.parametrize(
    "condition,code",
    [
        ("paused", "activation_not_ready"),
        ("unavailable", "activation_not_ready"),
        ("ci", "activation_not_ready"),
        ("rules", "activation_not_ready"),
        ("prompt", "prompt_not_seeded"),
    ],
)
def test_activation_uses_current_readiness_without_mutation(
    management_database: tuple[str, str, UUID],
    condition: str,
    code: str,
) -> None:
    async def exercise() -> None:
        async with activation_rig(management_database) as rig:
            installation = await _create(rig.management, management_database[2])
            await rig.management.bind_project(installation_id=installation, project_id=42)
            queries = {
                "paused": "UPDATE repositories SET enabled=false",
                "unavailable": "UPDATE repositories SET installation_available=false",
                "ci": "UPDATE repositories SET wait_for_ci='always'",
                "rules": "UPDATE rule_versions SET is_active=false",
                "prompt": "UPDATE prompt_versions SET is_active=false",
            }
            await rig.sql(queries[condition])
            before = await rig.state()
            with pytest.raises(GitLabManagementError, match=code) as failure:
                await rig.lifecycle.activate(installation_id=installation, expected_generation=1)
            if condition == "prompt":
                assert (
                    failure.value.remediation == "run uv run python -m app.bootstrap.seed_prompts"
                )
            assert await rig.state() == before

    asyncio.run(exercise())


@pytest.mark.parametrize(
    "race,code",
    [
        ("rotate", "connection_changed"),
        ("revoke", "connection_changed"),
        ("rebind", "connection_changed"),
        ("pause", "activation_not_ready"),
    ],
)
def test_activation_rechecks_snapshot_and_configuration_after_provider_io(
    management_database: tuple[str, str, UUID],
    race: str,
    code: str,
) -> None:
    async def exercise() -> None:
        async with activation_rig(management_database) as rig:
            installation = await _create(rig.management, management_database[2])
            await rig.management.bind_project(installation_id=installation, project_id=42)

            async def change() -> None:
                if race == "rotate":
                    await rig.lifecycle.rotate_token(installation_id=installation, token=_TOKEN)
                elif race == "revoke":
                    await rig.lifecycle.revoke_installation(installation_id=installation)
                elif race == "rebind":
                    await rig.management.bind_project(installation_id=installation, project_id=43)
                else:
                    await rig.sql("UPDATE repositories SET enabled=false")

            rig.before_project = change
            with pytest.raises(GitLabManagementError, match=code):
                await rig.lifecycle.activate(installation_id=installation, expected_generation=1)
            assert await rig.sql("SELECT intake_enabled FROM gitlab_connections") == [(False,)]
            assert await rig.sql("SELECT count(*) FROM runs") == [(0,)]

    asyncio.run(exercise())


@pytest.mark.parametrize(
    "failure,code",
    [
        ("pat", "invalid_token"),
        ("tls", "project_access_failed"),
        ("codec", "credential_operation_failed"),
    ],
)
def test_activation_rejects_stored_authentication_or_tls_failure_without_leak(
    management_database: tuple[str, str, UUID],
    failure: str,
    code: str,
) -> None:
    async def exercise() -> None:
        async with activation_rig(management_database) as rig:
            installation = await _create(rig.management, management_database[2])
            await rig.management.bind_project(installation_id=installation, project_id=42)
            if failure == "codec":
                await rig.sql("UPDATE gitlab_connections SET token_ciphertext=decode('00','hex')")
            rig.failure = failure
            before = await rig.state()
            with pytest.raises(GitLabManagementError, match=code) as error:
                await rig.lifecycle.activate(installation_id=installation, expected_generation=1)
            assert str(error.value) == code
            assert await rig.state() == before

    asyncio.run(exercise())


def test_activation_database_failure_rolls_back_flag_and_all_other_rows(
    management_database: tuple[str, str, UUID],
) -> None:
    from sqlalchemy.exc import DBAPIError

    async def exercise() -> None:
        async with activation_rig(management_database) as rig:
            installation = await _create(rig.management, management_database[2])
            await rig.management.bind_project(installation_id=installation, project_id=42)
            await rig.sql(
                "CREATE FUNCTION refuse_activation() RETURNS trigger LANGUAGE plpgsql AS $$ "
                "BEGIN RAISE EXCEPTION 'synthetic write failure'; END $$"
            )
            await rig.sql(
                "CREATE TRIGGER refuse_activation BEFORE UPDATE ON gitlab_connections "
                "FOR EACH ROW EXECUTE FUNCTION refuse_activation()"
            )
            before = await rig.state()
            with pytest.raises(DBAPIError):
                await rig.lifecycle.activate(installation_id=installation, expected_generation=1)
            assert await rig.state() == before

    asyncio.run(exercise())
