"""Real PostgreSQL request authority; enabled synthetic state is fixture setup only."""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from alembic.config import Config
from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.schema import CreateSchema, DropSchema

from alembic import command
from app.modules.integrations.gitlab.application.manage_connection import (
    CredentialEnvelope,
    GitLabConnectionLifecycle,
    GitLabOriginPolicy,
    ManageGitLabConnection,
)
from app.modules.integrations.gitlab.application.request_credentials import (
    BoundGitLabTarget,
    GitLabCredentialGenerationChanged,
    GitLabCredentialUnavailable,
    GitLabRequestCredentials,
    GitLabRequestUnavailable,
    GitLabTargetIdentity,
    GitLabTargetInactive,
)
from app.modules.integrations.gitlab.infrastructure.connection_store import (
    CredentialCipherAdapter,
    SqlAlchemyGitLabManagementUnitOfWork,
)
from app.modules.integrations.gitlab.infrastructure.credential_cipher import CredentialCipher
from app.modules.integrations.gitlab.infrastructure.request_credentials import (
    SqlAlchemyGitLabTargetReader,
)
from app.modules.repositories.application.installation_repositories import RepositorySnapshot
from app.modules.repositories.application.onboard_repository import load_default_rule_sets
from app.modules.repositories.application.sync_installation_repositories import (
    RepositoryOnboardingInput,
)

pytestmark = pytest.mark.integration
_TOKEN = "synthetic-authority-PAT-canary"
_ORIGIN = "https://gitlab.com"


@pytest.fixture
def authority_database() -> Iterator[tuple[str, str, UUID]]:
    database_url = os.environ.get("TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("set TEST_DATABASE_URL to run PostgreSQL integration tests")
    schema = f"test_gitlab_request_authority_{uuid4().hex}"
    workspace = uuid4()
    engine = create_engine(database_url, hide_parameters=True)
    try:
        with engine.connect() as connection:
            connection.execute(CreateSchema(schema))
            connection.commit()
            try:
                connection.execute(text(f'SET search_path TO "{schema}"'))
                connection.commit()
                config = Config("alembic.ini")
                config.attributes["connection"] = connection
                command.upgrade(config, "head")
                connection.execute(
                    text(
                        "INSERT INTO workspaces (id,name,daily_budget_usd) "
                        "VALUES (:id,'synthetic authority',7)"
                    ),
                    {"id": workspace},
                )
                connection.execute(
                    text(
                        "INSERT INTO prompt_versions (id,key,version,content,checksum,is_active) "
                        "VALUES (:id,'review.system',1,'synthetic prompt',:checksum,true)"
                    ),
                    {"id": uuid4(), "checksum": "a" * 64},
                )
                connection.commit()
                yield database_url, schema, workspace
            finally:
                connection.rollback()
                connection.execute(text("SET search_path TO public"))
                connection.execute(DropSchema(schema, cascade=True))
                connection.commit()
    finally:
        engine.dispose()


class Projects:
    async def authenticate(self, *, origin: str, token: str) -> None:
        assert origin in {_ORIGIN, "https://other.gitlab.example"}
        assert token in {_TOKEN, "replacement-authority-token"}

    async def fetch_project(
        self,
        *,
        origin: str,
        token: str,
        project_id: int,
    ) -> RepositoryOnboardingInput:
        await self.authenticate(origin=origin, token=token)
        return RepositoryOnboardingInput(
            RepositorySnapshot(project_id, "team/project", "main", f"{origin}/team/project"),
            {"Python": 100},
        )


@dataclass
class Rig:
    engine: AsyncEngine
    sessions: async_sessionmaker[AsyncSession]
    codec: CredentialCipherAdapter
    reader: SqlAlchemyGitLabTargetReader
    authority: GitLabRequestCredentials
    management: ManageGitLabConnection
    lifecycle: GitLabConnectionLifecycle
    workspace: UUID

    async def seed(
        self, *, origin: str = _ORIGIN, workspace: UUID | None = None
    ) -> BoundGitLabTarget:
        installation = uuid4()
        workspace = workspace or self.workspace
        await self.management.create(
            installation_id=installation,
            workspace_id=workspace,
            instance_origin=origin,
            token=_TOKEN,
            webhook_secret="s" * 32,
        )
        bound = await self.management.bind_project(installation_id=installation, project_id=17)
        # Fixture-only synthetic enabled state. No production enable operation is introduced.
        async with self.sessions() as session:
            await session.execute(
                text(
                    "UPDATE gitlab_connections SET intake_enabled=true "
                    "WHERE provider_installation_id=:id"
                ),
                {"id": installation},
            )
            await session.commit()
        return BoundGitLabTarget(installation, workspace, bound.repository_id, origin, 17, 1)


@asynccontextmanager
async def rig(database: tuple[str, str, UUID]) -> AsyncIterator[Rig]:
    database_url, schema, workspace = database
    engine = create_async_engine(
        database_url,
        connect_args={"options": f"-csearch_path={schema}"},
        hide_parameters=True,
    )
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    codec = CredentialCipherAdapter(
        CredentialCipher(active_key_id="test-key", keys={"test-key": b"k" * 32})
    )
    reader = SqlAlchemyGitLabTargetReader(sessions)
    policy = GitLabOriginPolicy(frozenset({_ORIGIN, "https://other.gitlab.example"}))
    try:
        yield Rig(
            engine,
            sessions,
            codec,
            reader,
            GitLabRequestCredentials(reader, codec),
            ManageGitLabConnection(
                uow_factory=lambda: SqlAlchemyGitLabManagementUnitOfWork(sessions),
                codec=codec,
                projects=Projects(),
                origin_policy=policy,
                rule_sets=load_default_rule_sets(Path("review/rules")),
            ),
            GitLabConnectionLifecycle(
                uow_factory=lambda: SqlAlchemyGitLabManagementUnitOfWork(sessions),
                codec=codec,
                projects=Projects(),
                origin_policy=policy,
            ),
            workspace,
        )
    finally:
        await engine.dispose()


def test_metadata_resolves_only_exact_bound_origin_and_decrypts_real_aad(
    authority_database: tuple[str, str, UUID],
) -> None:
    async def exercise() -> None:
        async with rig(authority_database) as fixture:
            target = await fixture.seed()
            result = await fixture.reader.resolve_target(
                GitLabTargetIdentity(target.origin_provider_installation_id, 17)
            )
            assert result == target
            assert set(vars(result)) == {
                "origin_provider_installation_id",
                "workspace_id",
                "repository_id",
                "instance_origin",
                "project_id",
                "credential_generation",
            }
            value = await fixture.authority.credential_for(result)
            assert value.token == _TOKEN
            assert _TOKEN not in repr(value)
            with pytest.raises(GitLabTargetInactive):
                await fixture.reader.resolve_target(GitLabTargetIdentity(uuid4(), 17))
            with pytest.raises(GitLabTargetInactive):
                await fixture.reader.resolve_target(
                    GitLabTargetIdentity(target.origin_provider_installation_id, 18)
                )

    asyncio.run(exercise())


async def mutation(fixture: Rig, statement: str, parameters: dict[str, Any]) -> None:
    """Fixture-only setup for states lacking an operator mutation API."""
    async with fixture.sessions() as session:
        await session.execute(text(statement), parameters)
        await session.commit()


def test_public_rotation_stops_pinned_attempt_but_fresh_attempt_uses_new_generation(
    authority_database: tuple[str, str, UUID],
) -> None:
    async def exercise() -> None:
        async with rig(authority_database) as fixture:
            old = await fixture.seed()
            credential = await fixture.authority.credential_for(old)
            await fixture.lifecycle.rotate_token(
                installation_id=old.origin_provider_installation_id,
                token="replacement-authority-token",
                expected_generation=1,
            )
            with pytest.raises(GitLabCredentialGenerationChanged):
                await fixture.authority.checkpoint(credential.target)
            with pytest.raises(GitLabCredentialGenerationChanged):
                await fixture.authority.credential_for(old)
            current = await fixture.reader.resolve_target(
                GitLabTargetIdentity(old.origin_provider_installation_id, 17),
            )
            assert current == replace(old, credential_generation=2)
            assert (
                await fixture.authority.credential_for(current)
            ).token == "replacement-authority-token"

    asyncio.run(exercise())


@pytest.mark.parametrize(
    "state",
    [
        "disabled",
        "unavailable",
        "deleted",
        "revoked",
        "missing_pair",
        "intake_off",
        "unbound",
    ],
)
def test_inactive_authority_refuses_metadata_envelope_and_checkpoint(
    authority_database: tuple[str, str, UUID],
    state: str,
) -> None:
    async def exercise() -> None:
        async with rig(authority_database) as fixture:
            target = await fixture.seed()
            parameters = {
                "origin": target.origin_provider_installation_id,
                "repo": target.repository_id,
            }
            changes = {
                "disabled": "UPDATE repositories SET enabled=false WHERE id=:repo",
                "unavailable": (
                    "UPDATE repositories SET installation_available=false WHERE id=:repo"
                ),
                "deleted": "UPDATE provider_installations SET deleted_at=now() WHERE id=:origin",
                "revoked": "UPDATE gitlab_connections SET "
                "revoked_at=now(),intake_enabled=false WHERE "
                "provider_installation_id=:origin",
                "missing_pair": "UPDATE gitlab_connections SET "
                "token_ciphertext=NULL,credential_key_id=NULL,"
                "revoked_at=now(),intake_enabled=false "
                "WHERE provider_installation_id=:origin",
                "intake_off": "UPDATE gitlab_connections SET intake_enabled=false WHERE "
                "provider_installation_id=:origin",
                "unbound": "DELETE FROM gitlab_project_bindings WHERE repository_id=:repo",
            }
            await mutation(fixture, changes[state], parameters)
            with pytest.raises(GitLabTargetInactive):
                await fixture.reader.resolve_target(
                    GitLabTargetIdentity(target.origin_provider_installation_id, 17)
                )
            with pytest.raises(GitLabTargetInactive):
                await fixture.authority.credential_for(target)
            with pytest.raises(GitLabTargetInactive):
                await fixture.authority.checkpoint(target)

    asyncio.run(exercise())


def test_other_tenant_other_instance_and_github_numeric_collision_cannot_select_credentials(
    authority_database: tuple[str, str, UUID],
) -> None:
    async def exercise() -> None:
        async with rig(authority_database) as fixture:
            target = await fixture.seed()
            other_workspace = uuid4()
            await mutation(
                fixture,
                "INSERT INTO workspaces (id,name,daily_budget_usd) VALUES (:id,'other',7)",
                {"id": other_workspace},
            )
            other = await fixture.seed(
                origin="https://other.gitlab.example", workspace=other_workspace
            )
            await fixture.lifecycle.rotate_token(
                installation_id=other.origin_provider_installation_id,
                token="replacement-authority-token",
            )
            other = await fixture.reader.resolve_target(
                GitLabTargetIdentity(other.origin_provider_installation_id, 17)
            )
            assert other.workspace_id == other_workspace
            assert other.instance_origin == "https://other.gitlab.example"
            assert (await fixture.authority.credential_for(target)).token == _TOKEN
            assert (
                await fixture.authority.credential_for(other)
            ).token == "replacement-authority-token"
            for forged in (
                replace(target, workspace_id=other_workspace),
                replace(target, repository_id=other.repository_id),
                replace(target, instance_origin="https://other.gitlab.example"),
                replace(target, project_id=18),
            ):
                with pytest.raises(GitLabTargetInactive):
                    await fixture.authority.credential_for(forged)
                with pytest.raises(GitLabTargetInactive):
                    await fixture.reader.checkpoint(forged)
            github = uuid4()
            await mutation(
                fixture,
                "INSERT INTO provider_installations "
                "(id,workspace_id,provider,external_id,metadata) "
                "SELECT :id,workspace_id,'github',external_id,'{}'::jsonb "
                "FROM provider_installations WHERE id=:origin",
                {"id": github, "origin": target.origin_provider_installation_id},
            )
            with pytest.raises(GitLabTargetInactive):
                await fixture.reader.resolve_target(GitLabTargetIdentity(github, 17))

    asyncio.run(exercise())


@pytest.mark.parametrize("damage", ["installation", "workspace", "origin", "key", "tamper"])
def test_wrong_key_aad_or_tamper_is_sanitized_credential_failure(
    authority_database: tuple[str, str, UUID],
    damage: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def exercise() -> None:
        async with rig(authority_database) as fixture:
            target = await fixture.seed()
            authority = fixture.authority
            if damage == "key":
                authority = GitLabRequestCredentials(
                    fixture.reader,
                    CredentialCipherAdapter(
                        CredentialCipher(active_key_id="unrelated", keys={"unrelated": b"z" * 32}),
                    ),
                )
            else:
                envelope = fixture.codec.encrypt(
                    _TOKEN,
                    installation_id=uuid4()
                    if damage == "installation"
                    else target.origin_provider_installation_id,
                    workspace_id=uuid4() if damage == "workspace" else target.workspace_id,
                    instance_origin="https://other.gitlab.example"
                    if damage == "origin"
                    else target.instance_origin,
                )
                ciphertext = envelope.ciphertext
                if damage == "tamper":
                    ciphertext = ciphertext[:-1] + bytes([ciphertext[-1] ^ 1])
                await mutation(
                    fixture,
                    "UPDATE gitlab_connections SET token_ciphertext=:ciphertext WHERE "
                    "provider_installation_id=:id",
                    {"ciphertext": ciphertext, "id": target.origin_provider_installation_id},
                )
            with pytest.raises(GitLabCredentialUnavailable) as caught:
                await authority.credential_for(target)
            assert str(caught.value) == "gitlab_credential_unavailable"
            assert _TOKEN not in repr(caught.value) + caplog.text
            assert "test-key" not in repr(caught.value) + caplog.text

    asyncio.run(exercise())


async def change_authority(fixture: Rig, target: BoundGitLabTarget, change: str) -> None:
    if change == "rotate":
        await fixture.lifecycle.rotate_token(
            installation_id=target.origin_provider_installation_id,
            token="replacement-authority-token",
            expected_generation=1,
        )
    elif change == "revoke":
        await fixture.lifecycle.revoke_installation(
            installation_id=target.origin_provider_installation_id
        )
    else:
        # Fixture-only replacement of a current reservation, not an operator rebind API.
        # The old repository remains historical and still belongs to the old parent.
        async with fixture.sessions() as session:
            await session.execute(
                text("DELETE FROM gitlab_project_bindings WHERE repository_id=:repo"),
                {"repo": target.repository_id},
            )
            await session.execute(
                text("UPDATE repositories SET external_id=18 WHERE id=:repo"),
                {"repo": target.repository_id},
            )
            await session.commit()
        await fixture.management.bind_project(
            installation_id=target.origin_provider_installation_id, project_id=17
        )


class InterleavingReader:
    def __init__(self, fixture: Rig, target: BoundGitLabTarget, change: str) -> None:
        self.fixture = fixture
        self.target = target
        self.change = change

    async def resolve_target(self, identity: GitLabTargetIdentity) -> BoundGitLabTarget:
        return await self.fixture.reader.resolve_target(identity)

    async def checkpoint(self, target: BoundGitLabTarget) -> None:
        await self.fixture.reader.checkpoint(target)

    async def credential_envelope_for(self, target: BoundGitLabTarget) -> CredentialEnvelope:
        envelope = await self.fixture.reader.credential_envelope_for(target)
        # Actual public lifecycle happens AFTER detached envelope read, BEFORE decrypt.
        await change_authority(self.fixture, self.target, self.change)
        return envelope


@pytest.mark.parametrize("change", ["rotate", "revoke", "rebind"])
@pytest.mark.parametrize("boundary", ["before_credential", "after_envelope", "after_credential"])
def test_authority_changes_at_request_boundaries_refuse_without_affecting_other_origin(
    authority_database: tuple[str, str, UUID],
    change: str,
    boundary: str,
) -> None:
    async def exercise() -> None:
        async with rig(authority_database) as fixture:
            target = await fixture.seed()
            other = await fixture.seed(origin="https://other.gitlab.example")
            expected = (
                GitLabCredentialGenerationChanged if change == "rotate" else GitLabTargetInactive
            )
            if boundary == "after_envelope":
                authority = GitLabRequestCredentials(
                    InterleavingReader(fixture, target, change), fixture.codec
                )
                with pytest.raises(expected):
                    await authority.credential_for(target)
            elif boundary == "before_credential":
                await change_authority(fixture, target, change)
                with pytest.raises(expected):
                    await fixture.authority.credential_for(target)
            else:
                value = await fixture.authority.credential_for(target)
                await change_authority(fixture, target, change)
                with pytest.raises(expected):
                    await fixture.authority.checkpoint(value.target)
            assert (await fixture.authority.credential_for(other)).token == _TOKEN

    asyncio.run(exercise())


def test_query_failure_is_operational_unavailable_and_not_permanent_or_tamper(
    authority_database: tuple[str, str, UUID],
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def exercise() -> None:
        async with rig(authority_database) as fixture:
            target = await fixture.seed()
            await mutation(
                fixture, "ALTER TABLE gitlab_project_bindings RENAME TO unavailable_bindings", {}
            )
            with pytest.raises(GitLabRequestUnavailable) as caught:
                await fixture.authority.credential_for(target)
            assert str(caught.value) == "gitlab_request_unavailable"
            for canary in (_TOKEN, "unavailable_bindings", "SELECT", "dmc169_test_only"):
                assert canary not in str(caught.value) + repr(caught.value) + caplog.text

    asyncio.run(exercise())


def assert_detached_no_transaction_or_parent_locks(
    database: tuple[str, str, UUID],
    target: BoundGitLabTarget,
) -> None:
    database_url, schema, _ = database
    observer = create_engine(
        database_url, hide_parameters=True, connect_args={"options": f"-csearch_path={schema}"}
    )
    try:
        with observer.connect() as connection:
            assert (
                connection.scalar(
                    text(
                        "SELECT count(*) FROM pg_stat_activity WHERE datname=current_database() "
                        "AND backend_type='client backend' AND pid<>pg_backend_pid() "
                        "AND xact_start IS NOT NULL",
                    )
                )
                == 0
            )
            # A new connection can lock every management parent immediately: reader held none.
            row = connection.execute(
                text(
                    "SELECT p.id FROM workspaces w JOIN provider_installations p ON "
                    "p.workspace_id=w.id "
                    "JOIN gitlab_connections c ON c.provider_installation_id=p.id "
                    "JOIN gitlab_project_bindings b ON b.provider_installation_id=p.id "
                    "JOIN repositories r ON r.id=b.repository_id WHERE p.id=:id FOR "
                    "UPDATE NOWAIT",
                ),
                {"id": target.origin_provider_installation_id},
            ).one()
            assert row[0] == target.origin_provider_installation_id
    finally:
        observer.dispose()


class ObservedCodec:
    def __init__(
        self,
        fixture: Rig,
        database: tuple[str, str, UUID],
        target: BoundGitLabTarget,
        change: str | None = None,
    ) -> None:
        self.fixture = fixture
        self.database = database
        self.target = target
        self.change = change
        self.decrypts = 0

    def encrypt(
        self, token: str, *, installation_id: UUID, workspace_id: UUID, instance_origin: str
    ) -> CredentialEnvelope:
        return self.fixture.codec.encrypt(
            token,
            installation_id=installation_id,
            workspace_id=workspace_id,
            instance_origin=instance_origin,
        )

    def webhook_digest(self, secret: str) -> str:
        return self.fixture.codec.webhook_digest(secret)

    def decrypt(
        self,
        credential: CredentialEnvelope,
        *,
        installation_id: UUID,
        workspace_id: UUID,
        instance_origin: str,
    ) -> str:
        assert_detached_no_transaction_or_parent_locks(self.database, self.target)
        token = self.fixture.codec.decrypt(
            credential,
            installation_id=installation_id,
            workspace_id=workspace_id,
            instance_origin=instance_origin,
        )
        self.decrypts += 1
        if self.change is not None:

            async def change_in_own_loop() -> None:
                async with rig(self.database) as independent:
                    await change_authority(independent, self.target, self.change or "")

            # The synchronous pure codec seam admits a deterministic lifecycle mutation
            # while decrypt is in progress; the mutator owns a separate loop/engine.
            with ThreadPoolExecutor(max_workers=1) as executor:
                executor.submit(asyncio.run, change_in_own_loop()).result(timeout=10)
        return token


def test_metadata_selects_no_secrets_and_reads_close_before_decrypt_and_escape(
    authority_database: tuple[str, str, UUID],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def exercise() -> None:
        async with rig(authority_database) as fixture:
            target = await fixture.seed()
            for name in (
                "GITLAB_CREDENTIAL_KEYS",
                "GITLAB_ACTIVE_KEY_ID",
                "GITLAB_ALLOWED_ORIGINS",
                "SSL_CERT_FILE",
                "GITLAB_TOKEN",
            ):
                monkeypatch.setenv(name, "broken-unused-configuration-canary")
            projections: list[str] = []
            transactions: set[int] = set()

            def began(connection: Connection) -> None:
                transactions.add(id(connection))

            def ended(connection: Connection) -> None:
                transactions.discard(id(connection))

            def observed(
                connection: Connection,
                cursor: Any,
                statement: str,
                parameters: Any,
                context: Any,
                executemany: bool,
            ) -> None:
                if "FROM gitlab_connections" in statement:
                    projections.append(statement.split("FROM", 1)[0])

            event.listen(fixture.engine.sync_engine, "begin", began)
            event.listen(fixture.engine.sync_engine, "commit", ended)
            event.listen(fixture.engine.sync_engine, "rollback", ended)
            event.listen(fixture.engine.sync_engine, "before_cursor_execute", observed)
            result = await fixture.reader.resolve_target(
                GitLabTargetIdentity(target.origin_provider_installation_id, 17)
            )
            await fixture.reader.checkpoint(result)
            assert transactions == set()
            assert len(projections) == 2
            for projection in projections:
                for secret_column in (
                    "token_ciphertext",
                    "credential_key_id",
                    "webhook_secret_digest",
                ):
                    assert secret_column not in projection
            assert_detached_no_transaction_or_parent_locks(authority_database, target)
            codec = ObservedCodec(fixture, authority_database, target)
            value = await GitLabRequestCredentials(fixture.reader, codec).credential_for(result)
            assert value.token == _TOKEN
            assert codec.decrypts == 1
            assert transactions == set()
            assert_detached_no_transaction_or_parent_locks(authority_database, target)

    asyncio.run(exercise())


@pytest.mark.parametrize("change", ["rotate", "revoke", "rebind"])
def test_actual_mutation_during_real_decrypt_refuses_before_credential_escapes(
    authority_database: tuple[str, str, UUID],
    change: str,
) -> None:
    async def exercise() -> None:
        async with rig(authority_database) as fixture:
            target = await fixture.seed()
            other = await fixture.seed(origin="https://other.gitlab.example")
            codec = ObservedCodec(fixture, authority_database, target, change)
            expected = (
                GitLabCredentialGenerationChanged if change == "rotate" else GitLabTargetInactive
            )
            with pytest.raises(expected):
                await GitLabRequestCredentials(fixture.reader, codec).credential_for(target)
            assert codec.decrypts == 1
            assert (await fixture.authority.credential_for(other)).token == _TOKEN

    asyncio.run(exercise())


def test_unexpected_driver_boundary_failure_remains_sanitized_operational_unavailable(
    authority_database: tuple[str, str, UUID],
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def exercise() -> None:
        async with rig(authority_database) as fixture:
            target = await fixture.seed()

            def failed_driver_boundary(
                connection: Connection,
                cursor: Any,
                statement: str,
                parameters: Any,
                context: Any,
                executemany: bool,
            ) -> None:
                raise RuntimeError("driver-secret-canary DSN-canary token_ciphertext")

            event.listen(
                fixture.engine.sync_engine, "before_cursor_execute", failed_driver_boundary
            )
            with pytest.raises(GitLabRequestUnavailable) as caught:
                await fixture.reader.resolve_target(
                    GitLabTargetIdentity(target.origin_provider_installation_id, 17),
                )
            assert str(caught.value) == "gitlab_request_unavailable"
            for canary in ("driver-secret-canary", "DSN-canary", "token_ciphertext"):
                assert canary not in str(caught.value) + repr(caught.value) + caplog.text

    asyncio.run(exercise())
