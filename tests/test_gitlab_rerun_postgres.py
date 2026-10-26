"""Trusted reruns preserve their original origin; portal grants remain GitHub-only."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field, replace
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
from sqlalchemy import Select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.modules.auth.application.scope import AuthScope
from app.modules.reviews.application.queue_messages import StoredRunMessage
from app.modules.reviews.application.rerun_run import (
    RerunConflict,
    RerunNotConfigured,
    RerunOriginConflict,
    RerunRun,
)
from app.modules.reviews.application.try_enqueue_webhook_run import (
    PendingRunMessage,
    RunPublicationKind,
)
from app.modules.reviews.infrastructure.models import CodeChange, Run
from app.modules.reviews.infrastructure.rerun_store import SqlAlchemyRerunUnitOfWork
from app.modules.reviews.infrastructure.run_repository import SqlAlchemyRunRepository
from tests import test_gitlab_projection_postgres as projection_fixtures
from tests.test_gitlab_projection_postgres import NOW, Rig, rig, run_json, seed_run

rerun_database = projection_fixtures.authority_database
pytestmark = pytest.mark.integration


async def seed_change(fixture: Rig, repository: UUID) -> UUID:
    change = uuid4()
    async with fixture.sessions.begin() as session:
        await session.execute(
            text(
                "INSERT INTO code_changes (id,repository_id,external_id,external_number,title,"
                "source_branch,target_branch,base_sha,head_sha,state,web_url) VALUES "
                "(:id,:repo,901,12,'Rerun MR','feature','main',:base,:head,'open',"
                "'https://gitlab.com/team/project/-/merge_requests/12')"
            ),
            {"id": change, "repo": repository, "base": "b" * 40, "head": "d" * 40},
        )
    return change


@dataclass
class Publisher:
    messages: list[PendingRunMessage] = field(default_factory=list)

    async def publish_confirmed(
        self, message: PendingRunMessage, *, kind: RunPublicationKind = RunPublicationKind.QUEUED
    ) -> None:
        self.messages.append(message)


def rerun(sessions: async_sessionmaker[AsyncSession], publisher: Publisher) -> RerunRun:
    return RerunRun(
        lambda: SqlAlchemyRerunUnitOfWork(sessions, allow_unscoped=True),
        SqlAlchemyRunRepository(sessions, allow_unscoped=True),
        publisher,
        now=lambda: NOW,
    )


@dataclass
class OriginalReadBarrier:
    reached: asyncio.Event = field(default_factory=asyncio.Event)
    release: asyncio.Event = field(default_factory=asyncio.Event)
    reads: int = 0

    async def after(self, statement: Any) -> None:
        # Public SQLAlchemy query metadata covers scalar and tuple reference reads.
        if not isinstance(statement, Select) or not statement.column_descriptions:
            return
        first = statement.column_descriptions[0]
        if first.get("entity") is Run and first.get("name") == "code_change_id":
            self.reads += 1
            self.reached.set()
            await asyncio.wait_for(self.release.wait(), 5)

    def sessions(self, fixture: Rig) -> async_sessionmaker[AsyncSession]:
        barrier = self

        class ReferenceSession(AsyncSession):
            async def scalar(self, statement: Any, *args: Any, **kwargs: Any) -> Any:
                result = await super().scalar(statement, *args, **kwargs)
                await barrier.after(statement)
                return result

            async def execute(self, statement: Any, *args: Any, **kwargs: Any) -> Any:
                result = await super().execute(statement, *args, **kwargs)
                await barrier.after(statement)
                return result

        return async_sessionmaker[AsyncSession](
            fixture.engine, class_=ReferenceSession, expire_on_commit=False
        )


def test_public_rerun_refuses_replacement_committed_after_original_run_read(
    rerun_database: tuple[str, str, UUID],
) -> None:
    async def scenario() -> None:
        async with rig(rerun_database) as fixture, rig(rerun_database) as independent:
            original = await fixture.seed()
            change = await seed_change(fixture, original.repository_id)
            old_run = await seed_run(fixture, original, change, state="succeeded")
            barrier = OriginalReadBarrier()
            publisher = Publisher()
            task = asyncio.create_task(rerun(barrier.sessions(fixture), publisher).execute(old_run))
            with psycopg.connect(
                rerun_database[0].replace("postgresql+psycopg://", "postgresql://"),
                autocommit=True,
            ) as observer:
                observer.execute("LISTEN run_updated")
                try:
                    await asyncio.wait_for(barrier.reached.wait(), 3)
                    # Real management retirement commits before current target resolution.
                    await independent.lifecycle.revoke_installation(
                        installation_id=original.origin_provider_installation_id
                    )
                    replacement = await independent.seed()
                    # Adversarial historical rebind, not a supported management operation.
                    async with independent.sessions.begin() as session:
                        await session.execute(
                            text("UPDATE code_changes SET repository_id=:repo WHERE id=:change"),
                            {"repo": replacement.repository_id, "change": change},
                        )
                    barrier.release.set()
                    with pytest.raises(RerunOriginConflict):
                        await asyncio.wait_for(task, 5)
                    assert barrier.reads == 1
                    assert publisher.messages == []
                    assert list(observer.notifies(timeout=0.02, stop_after=1)) == []
                    async with fixture.sessions() as session:
                        assert await session.scalar(text("SELECT count(*) FROM runs")) == 1
                        assert (
                            await session.scalar(
                                text(
                                    "SELECT origin_provider_installation_id FROM runs WHERE id=:id"
                                ),
                                {"id": old_run},
                            )
                            == original.origin_provider_installation_id
                        )
                finally:
                    barrier.release.set()
                    if not task.done():
                        task.cancel()
                    await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())


@dataclass
class CommittedPublisher:
    fixture: Rig
    database_url: str
    origin: UUID
    provider: str
    messages: list[PendingRunMessage] = field(default_factory=list)

    async def publish_confirmed(
        self, message: PendingRunMessage, *, kind: RunPublicationKind = RunPublicationKind.QUEUED
    ) -> None:
        # SAME pool1 can read the committed row only after the insertion UOW has exited.
        async with self.fixture.sessions() as session:
            row = (
                await session.execute(
                    text(
                        "SELECT r.origin_provider_installation_id,pi.provider,pi.workspace_id,"
                        "r.head_sha,r.base_sha,r.base_ref,r.trigger,r.message_published_at "
                        "FROM runs r JOIN provider_installations pi "
                        "ON pi.id=r.origin_provider_installation_id WHERE r.id=:id"
                    ),
                    {"id": message.run_id},
                )
            ).one()
            assert tuple(row) == (
                self.origin,
                self.provider,
                self.fixture.workspace,
                "d" * 40,
                "b" * 40,
                "main",
                "rerun",
                None,
            )
        # This exclusive happy-case observation does not count a deliberate retirement peer.
        with psycopg.connect(
            self.database_url.replace("postgresql+psycopg://", "postgresql://"),
            autocommit=True,
        ) as observer:
            assert observer.execute(
                "SELECT count(*) FROM pg_stat_activity WHERE datname=current_database() "
                "AND backend_type='client backend' AND pid<>pg_backend_pid() "
                "AND xact_start IS NOT NULL"
            ).fetchone() == (0,)
        assert (message.provider, message.head_sha, message.base_sha, message.base_ref) == (
            self.provider,
            "d" * 40,
            "b" * 40,
            "main",
        )
        assert (message.workspace_id, message.repository_external_id, message.pr_number) == (
            self.fixture.workspace,
            17,
            12,
        )
        assert isinstance(message, StoredRunMessage)
        assert message.trigger == "rerun"
        self.messages.append(message)


async def seed_github(
    fixture: Rig, template: UUID, workspace: UUID, external: int
) -> tuple[UUID, UUID]:
    origin, repository = uuid4(), uuid4()
    async with fixture.sessions.begin() as session:
        await session.execute(
            text(
                "INSERT INTO provider_installations "
                "(id,workspace_id,provider,external_id,metadata) "
                "VALUES (:id,:ws,'github',:external,'{}')"
            ),
            {"id": origin, "ws": workspace, "external": external},
        )
        await session.execute(
            text(
                "INSERT INTO repositories (id,provider_installation_id,external_id,full_name,"
                "default_branch,web_url,wait_for_ci) VALUES (:id,:origin,17,'team/project','main',"
                "'https://github.com/team/project','never')"
            ),
            {"id": repository, "origin": origin},
        )
        await session.execute(
            text(
                "INSERT INTO rule_versions (id,repository_id,version,rules,checksum,is_active) "
                "SELECT :id,:repo,version,rules,checksum,true FROM rule_versions "
                "WHERE repository_id=:template AND is_active"
            ),
            {"id": uuid4(), "repo": repository, "template": template},
        )
    return origin, repository


@pytest.mark.parametrize(
    ("provider", "rotate"), [("gitlab", False), ("gitlab", True), ("github", False)]
)
def test_public_rerun_uses_current_head_original_origin_and_committed_pool_one_publish(
    rerun_database: tuple[str, str, UUID],
    provider: str,
    rotate: bool,
) -> None:
    async def scenario() -> None:
        async with rig(rerun_database) as fixture:
            target = await fixture.seed()
            change = await seed_change(fixture, target.repository_id)
            old = await seed_run(fixture, target, change, state="succeeded")
            origin = target.origin_provider_installation_id
            if provider == "github":
                # A valid GitHub source, with colliding numeric project/installation IDs.
                async with fixture.sessions() as session:
                    external = await session.scalar(
                        text("SELECT external_id FROM provider_installations WHERE id=:id"),
                        {"id": origin},
                    )
                assert isinstance(external, int)
                origin, repository = await seed_github(
                    fixture,
                    target.repository_id,
                    fixture.workspace,
                    external,
                )
                async with fixture.sessions.begin() as session:
                    await session.execute(
                        text("UPDATE code_changes SET repository_id=:id"), {"id": repository}
                    )
                    await session.execute(
                        text("UPDATE runs SET origin_provider_installation_id=:id WHERE id=:run"),
                        {"id": origin, "run": old},
                    )
            if rotate:
                # Rotation retires work, but a terminal history can start a NEW trusted attempt.
                await fixture.lifecycle.rotate_token(
                    installation_id=target.origin_provider_installation_id,
                    token="replacement-authority-token",
                )
            publisher = CommittedPublisher(fixture, rerun_database[0], origin, provider)
            use_case = RerunRun(
                lambda: SqlAlchemyRerunUnitOfWork(fixture.sessions, allow_unscoped=True),
                SqlAlchemyRunRepository(fixture.sessions, allow_unscoped=True),
                publisher,
                now=lambda: NOW,
            )
            with psycopg.connect(
                rerun_database[0].replace("postgresql+psycopg://", "postgresql://"),
                autocommit=True,
            ) as observer:
                observer.execute("LISTEN run_updated")
                created = await asyncio.wait_for(use_case.execute(old), 5)
                assert created is not None and created.id != old
                assert (created.status, created.trigger, created.head_sha) == (
                    "queued",
                    "rerun",
                    "d" * 40,
                )
                notices = list(observer.notifies(timeout=0.05, stop_after=1))
                assert len(notices) == 1
                assert json.loads(notices[0].payload) == {
                    "run_id": str(created.id),
                    "workspace_id": str(fixture.workspace),
                    "status": "queued",
                }
            assert len(publisher.messages) == 1
            async with fixture.sessions() as session:
                assert (
                    await session.scalar(
                        text("SELECT message_published_at FROM runs WHERE id=:id"),
                        {"id": created.id},
                    )
                    == NOW
                )
                assert (
                    await session.scalar(
                        text("SELECT head_sha FROM runs WHERE id=:id"), {"id": old}
                    )
                    == "a" * 40
                )

    asyncio.run(scenario())


@pytest.mark.parametrize("original_provider", ["gitlab", "github"])
@pytest.mark.parametrize("replacement_provider", ["gitlab", "github"])
@pytest.mark.parametrize("other_workspace", [False, True])
def test_historical_rerun_cannot_adopt_current_provider_or_tenant_with_colliding_ids(
    rerun_database: tuple[str, str, UUID],
    original_provider: str,
    replacement_provider: str,
    other_workspace: bool,
) -> None:
    async def scenario() -> None:
        async with rig(rerun_database) as fixture:
            original = await fixture.seed()
            change = await seed_change(fixture, original.repository_id)
            old = await seed_run(fixture, original, change, state="succeeded")
            workspace = uuid4() if other_workspace else fixture.workspace
            if other_workspace:
                async with fixture.sessions.begin() as session:
                    await session.execute(
                        text(
                            "INSERT INTO workspaces (id,name,daily_budget_usd) "
                            "VALUES (:id,'other',7)"
                        ),
                        {"id": workspace},
                    )
            async with fixture.sessions() as session:
                number = await session.scalar(
                    text("SELECT external_id FROM provider_installations WHERE id=:id"),
                    {"id": original.origin_provider_installation_id},
                )
            assert isinstance(number, int)
            if original_provider == "github":
                github_origin, github_repository = await seed_github(
                    fixture,
                    original.repository_id,
                    fixture.workspace,
                    number,
                )
                async with fixture.sessions.begin() as session:
                    await session.execute(
                        text("UPDATE code_changes SET repository_id=:id"), {"id": github_repository}
                    )
                    await session.execute(
                        text("UPDATE runs SET origin_provider_installation_id=:id WHERE id=:run"),
                        {"id": github_origin, "run": old},
                    )
                # Same-provider installation IDs are unique; project/MR IDs still collide.
                number += 1
            if replacement_provider == "gitlab":
                replacement = await fixture.seed(
                    origin="https://other.gitlab.example", workspace=workspace
                )
                replacement_repository = replacement.repository_id
            else:
                _, replacement_repository = await seed_github(
                    fixture,
                    original.repository_id,
                    workspace,
                    number,
                )
            # Fixture-labelled historical CodeChange reparenting preserves original Run.origin.
            async with fixture.sessions.begin() as session:
                await session.execute(
                    text("UPDATE code_changes SET repository_id=:id WHERE id=:cc"),
                    {"id": replacement_repository, "cc": change},
                )
            publisher = Publisher()
            with psycopg.connect(
                rerun_database[0].replace("postgresql+psycopg://", "postgresql://"),
                autocommit=True,
            ) as observer:
                observer.execute("LISTEN run_updated")
                with pytest.raises(RerunOriginConflict):
                    await rerun(fixture.sessions, publisher).execute(old)
                assert list(observer.notifies(timeout=0.02, stop_after=1)) == []
            assert publisher.messages == []
            async with fixture.sessions() as session:
                assert await session.scalar(text("SELECT count(*) FROM runs")) == 1

    asyncio.run(scenario())


@pytest.mark.parametrize("condition", ["null", "deleted", "revoked", "unbound"])
def test_original_run_without_active_authority_does_not_rerun(
    rerun_database: tuple[str, str, UUID],
    condition: str,
) -> None:
    async def scenario() -> None:
        async with rig(rerun_database) as fixture:
            target = await fixture.seed()
            change = await seed_change(fixture, target.repository_id)
            old = await seed_run(fixture, target, change, state="succeeded")
            if condition == "revoked":
                await fixture.lifecycle.revoke_installation(
                    installation_id=target.origin_provider_installation_id,
                )
            else:
                statement = {
                    "null": "UPDATE runs SET origin_provider_installation_id=NULL",
                    "deleted": "UPDATE provider_installations SET deleted_at=clock_timestamp()",
                    "unbound": "DELETE FROM gitlab_project_bindings",
                }[condition]
                async with fixture.sessions.begin() as session:
                    await session.execute(text(statement))
            publisher = Publisher()
            with pytest.raises(RerunOriginConflict):
                await rerun(fixture.sessions, publisher).execute(old)
            assert publisher.messages == []
            async with fixture.sessions() as session:
                assert await session.scalar(text("SELECT count(*) FROM runs")) == 1

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("condition", "expected"),
    [
        ("closed", RerunConflict),
        ("active", RerunConflict),
        ("missing_rules", RerunOriginConflict),
        ("missing_prompt", RerunOriginConflict),
        ("github_paused", RerunConflict),
        ("github_paused_missing_rules", RerunOriginConflict),
        ("valid_origin_missing_rules", RerunNotConfigured),
        ("valid_origin_missing_prompt", RerunNotConfigured),
    ],
)
def test_rerun_authority_precedes_configuration_but_valid_origin_retains_422(
    rerun_database: tuple[str, str, UUID],
    condition: str,
    expected: type[Exception],
) -> None:
    async def scenario() -> None:
        async with rig(rerun_database) as fixture:
            target = await fixture.seed()
            change = await seed_change(fixture, target.repository_id)
            old = await seed_run(fixture, target, change, state="succeeded")
            if condition.startswith("github"):
                _, repository = await seed_github(
                    fixture,
                    target.repository_id,
                    fixture.workspace,
                    71,
                )
                async with fixture.sessions.begin() as session:
                    await session.execute(
                        text("UPDATE code_changes SET repository_id=:id"), {"id": repository}
                    )
            async with fixture.sessions.begin() as session:
                if not condition.startswith("valid_origin"):
                    await session.execute(
                        text("UPDATE runs SET origin_provider_installation_id=NULL")
                    )
                statements = {
                    "closed": ["UPDATE code_changes SET state='closed'"],
                    "active": ["UPDATE runs SET state='queued'"],
                    "missing_rules": ["UPDATE rule_versions SET is_active=false"],
                    "missing_prompt": [
                        "UPDATE repositories SET prompt_version_id=NULL",
                        "UPDATE prompt_versions SET is_active=false",
                    ],
                    "github_paused": ["UPDATE repositories SET enabled=false"],
                    "github_paused_missing_rules": [
                        "UPDATE repositories SET enabled=false",
                        "UPDATE rule_versions SET is_active=false",
                    ],
                }[condition.removeprefix("valid_origin_")]
                for statement in statements:
                    await session.execute(text(statement))
            publisher = Publisher()
            with pytest.raises(expected):
                await rerun(fixture.sessions, publisher).execute(old)
            assert publisher.messages == []
            async with fixture.sessions() as session:
                assert await session.scalar(text("SELECT count(*) FROM runs")) == 1

    asyncio.run(scenario())


def test_scoped_portal_cannot_read_or_rerun_gitlab_even_with_matching_github_grants(
    rerun_database: tuple[str, str, UUID],
) -> None:
    async def scenario() -> None:
        async with rig(rerun_database) as fixture:
            target = await fixture.seed()
            change = await seed_change(fixture, target.repository_id)
            old = await seed_run(fixture, target, change, state="succeeded")
            async with fixture.sessions.begin() as session:
                await session.execute(
                    text("INSERT INTO github_user_profiles (id,login) VALUES (42,'fixture')")
                )
                await session.execute(
                    text(
                        "INSERT INTO github_user_workspace_access (github_user_id,workspace_id) "
                        "VALUES (42,:workspace)"
                    ),
                    {"workspace": fixture.workspace},
                )
                await session.execute(
                    text(
                        "INSERT INTO github_user_repository_access (github_user_id,"
                        "provider_installation_id,repository_external_id) VALUES (42,:origin,17)"
                    ),
                    {"origin": target.origin_provider_installation_id},
                )
            scope = AuthScope(42, (fixture.workspace,))
            reader = SqlAlchemyRunRepository(fixture.sessions, scope)
            publisher = Publisher()
            use_case = RerunRun(
                lambda: SqlAlchemyRerunUnitOfWork(fixture.sessions, scope),
                reader,
                publisher,
                now=lambda: NOW,
            )
            assert await reader.get_run(old) is None
            assert await use_case.execute(old) is None
            assert publisher.messages == []
            async with fixture.sessions() as session:
                assert await session.scalar(text("SELECT count(*) FROM runs")) == 1

    asyncio.run(scenario())


def test_rerun_commit_before_management_retirement_serializes_without_deadlock(
    rerun_database: tuple[str, str, UUID],
) -> None:
    async def scenario() -> None:
        async with rig(rerun_database) as fixture, rig(rerun_database) as independent:
            target = await fixture.seed()
            change = await seed_change(fixture, target.repository_id)
            old = await seed_run(fixture, target, change, state="succeeded")
            locked, release = asyncio.Event(), asyncio.Event()
            owner_pid: int | None = None

            class LockedSession(AsyncSession):
                async def scalar(self, statement: Any, *args: Any, **kwargs: Any) -> Any:
                    nonlocal owner_pid
                    result = await super().scalar(statement, *args, **kwargs)
                    if isinstance(statement, Select) and statement.column_descriptions:
                        first = statement.column_descriptions[0]
                        if first.get("entity") is CodeChange and first.get("name") == "state":
                            owner_pid = await super().scalar(text("SELECT pg_backend_pid()"))
                            locked.set()
                            await asyncio.wait_for(release.wait(), 5)
                    return result

            sessions = async_sessionmaker[AsyncSession](
                fixture.engine, class_=LockedSession, expire_on_commit=False
            )
            use_case = RerunRun(
                lambda: SqlAlchemyRerunUnitOfWork(sessions, allow_unscoped=True),
                SqlAlchemyRunRepository(sessions, allow_unscoped=True),
                None,
                now=lambda: NOW,
            )
            tasks: list[asyncio.Task[Any]] = [asyncio.create_task(use_case.execute(old))]
            try:
                await asyncio.wait_for(locked.wait(), 3)
                assert owner_pid is not None
                tasks.append(
                    asyncio.create_task(
                        independent.lifecycle.revoke_installation(
                            installation_id=target.origin_provider_installation_id,
                        )
                    )
                )
                async with await psycopg.AsyncConnection.connect(
                    rerun_database[0].replace("postgresql+psycopg://", "postgresql://"),
                    autocommit=True,
                ) as observer:

                    async def wait_for_actual_block() -> None:
                        while True:
                            cursor = await observer.execute(
                                "SELECT EXISTS (SELECT 1 FROM pg_stat_activity "
                                "WHERE datname=current_database() "
                                "AND backend_type='client backend' "
                                "AND %s=ANY(pg_blocking_pids(pid)))",
                                (owner_pid,),
                            )
                            if await cursor.fetchone() == (True,):
                                return
                            await asyncio.sleep(0.01)

                    await asyncio.wait_for(wait_for_actual_block(), 3)
                release.set()
                created, _ = await asyncio.wait_for(asyncio.gather(*tasks), 5)
                assert created is not None and created.id != old
                async with fixture.sessions() as session:
                    row = (
                        await session.execute(
                            text(
                                "SELECT origin_provider_installation_id,state,error_code "
                                "FROM runs WHERE id=:id"
                            ),
                            {"id": created.id},
                        )
                    ).one()
                    assert tuple(row) == (
                        target.origin_provider_installation_id,
                        "cancelled",
                        "installation_revoked",
                    )
                    assert await session.scalar(text("SELECT count(*) FROM runs")) == 2
            finally:
                release.set()
                for task in tasks:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "lineage",
    [
        "same",
        "different_account",
        "missing_original_account",
        "missing_current_account",
        "different_workspace",
        "different_repository",
        "reparent_after_read",
        "original_provider",
        "original_not_retired",
    ],
)
def test_reinstalled_github_rerun_requires_same_account_workspace_and_original_repository(
    rerun_database: tuple[str, str, UUID], lineage: str
) -> None:
    async def scenario() -> None:
        async with rig(rerun_database) as fixture, rig(rerun_database) as independent:
            template = await fixture.seed()
            original, repository = await seed_github(
                fixture, template.repository_id, fixture.workspace, 71
            )
            change = await seed_change(fixture, repository)
            old = await seed_run(
                fixture,
                replace(template, repository_id=repository),
                change,
                state="succeeded",
                origin=original,
            )
            current = uuid4()
            # Synthetic OAuth rebind state. The existing HTTP regressions exercise
            # the supported transition; these public controls isolate its lineage.
            async with fixture.sessions.begin() as session:
                await session.execute(
                    text(
                        "UPDATE provider_installations SET "
                        "account_external_id=700,deleted_at=clock_timestamp() WHERE id=:id"
                    ),
                    {"id": original},
                )
                await session.execute(
                    text(
                        "INSERT INTO provider_installations "
                        "(id,workspace_id,provider,external_id,account_external_id,metadata) "
                        "VALUES (:id,:ws,'github',72,700,'{}')"
                    ),
                    {"id": current, "ws": fixture.workspace},
                )
                await session.execute(
                    text("UPDATE repositories SET provider_installation_id=:origin WHERE id=:id"),
                    {"origin": current, "id": repository},
                )
                if lineage == "different_account":
                    await session.execute(
                        text(
                            "UPDATE provider_installations SET account_external_id=701 WHERE id=:id"
                        ),
                        {"id": current},
                    )
                elif lineage in {"missing_original_account", "missing_current_account"}:
                    await session.execute(
                        text(
                            "UPDATE provider_installations SET "
                            "account_external_id=NULL WHERE id=:id"
                        ),
                        {"id": original if lineage == "missing_original_account" else current},
                    )
                elif lineage == "different_workspace":
                    workspace = uuid4()
                    await session.execute(
                        text(
                            "INSERT INTO workspaces(id,name,daily_budget_usd) "
                            "VALUES (:id,'other workspace',7)"
                        ),
                        {"id": workspace},
                    )
                    await session.execute(
                        text("UPDATE provider_installations SET workspace_id=:ws WHERE id=:id"),
                        {"ws": workspace, "id": current},
                    )
                elif lineage == "original_provider":
                    await session.execute(
                        text("UPDATE provider_installations SET provider='gitlab' WHERE id=:id"),
                        {"id": original},
                    )
                elif lineage == "original_not_retired":
                    await session.execute(
                        text("UPDATE provider_installations SET deleted_at=NULL WHERE id=:id"),
                        {"id": original},
                    )
            alternative = None
            if lineage in {"different_repository", "reparent_after_read"}:
                replacement, alternative = await seed_github(
                    fixture, repository, fixture.workspace, 73
                )
                async with fixture.sessions.begin() as session:
                    await session.execute(
                        text(
                            "UPDATE provider_installations SET account_external_id=700 WHERE id=:id"
                        ),
                        {"id": replacement},
                    )
                    if lineage == "different_repository":
                        await session.execute(
                            text("UPDATE code_changes SET repository_id=:repo WHERE id=:id"),
                            {"repo": alternative, "id": change},
                        )
            before = await run_json(fixture, old)
            publisher = Publisher()
            if lineage == "reparent_after_read":
                barrier = OriginalReadBarrier()
                task = asyncio.create_task(rerun(barrier.sessions(fixture), publisher).execute(old))
                try:
                    await asyncio.wait_for(barrier.reached.wait(), 3)
                    async with independent.sessions.begin() as session:
                        await session.execute(
                            text("UPDATE code_changes SET repository_id=:repo WHERE id=:id"),
                            {"repo": alternative, "id": change},
                        )
                    barrier.release.set()
                    with pytest.raises(RerunOriginConflict):
                        await asyncio.wait_for(task, 5)
                    assert barrier.reads == 1
                finally:
                    barrier.release.set()
                    if not task.done():
                        task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
            elif lineage == "same":
                created = await rerun(fixture.sessions, publisher).execute(old)
                assert created is not None and created.id != old
                assert (created.status, created.head_sha, created.trigger) == (
                    "queued",
                    "d" * 40,
                    "rerun",
                )
                assert len(publisher.messages) == 1
                async with fixture.sessions() as session:
                    assert (
                        await session.scalar(
                            text("SELECT origin_provider_installation_id FROM runs WHERE id=:id"),
                            {"id": created.id},
                        )
                        == current
                    )
            else:
                with pytest.raises(RerunOriginConflict):
                    await rerun(fixture.sessions, publisher).execute(old)
            assert await run_json(fixture, old) == before
            if lineage != "same":
                assert publisher.messages == []
                async with fixture.sessions() as session:
                    assert await session.scalar(text("SELECT count(*) FROM runs")) == 1

    asyncio.run(asyncio.wait_for(scenario(), 15))
