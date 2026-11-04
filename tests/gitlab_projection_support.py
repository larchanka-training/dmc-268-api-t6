"""Shared real-PostgreSQL GitLab projection fixtures and helpers."""

from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool
from sqlalchemy.schema import CreateSchema, DropSchema

from alembic import command
from app.modules.integrations.gitlab.api.dispatch import GitLabWebhookDispatchAdapter, action_of
from app.modules.integrations.gitlab.application.eligibility import DetermineGitLabEligibility
from app.modules.integrations.gitlab.application.manage_connection import (
    GitLabConnectionLifecycle,
    GitLabOriginPolicy,
    ManageGitLabConnection,
)
from app.modules.integrations.gitlab.application.request_credentials import (
    BoundGitLabTarget,
    GitLabRequestCredentials,
)
from app.modules.integrations.gitlab.application.webhook_receipts import prepare_gitlab_receipt
from app.modules.integrations.gitlab.infrastructure.connection_store import (
    CredentialCipherAdapter,
    SqlAlchemyGitLabManagementUnitOfWork,
)
from app.modules.integrations.gitlab.infrastructure.credential_cipher import CredentialCipher
from app.modules.integrations.gitlab.infrastructure.current_merge_request import (
    HttpGitLabCurrentMergeRequestProvider,
)
from app.modules.integrations.gitlab.infrastructure.request_credentials import (
    SqlAlchemyGitLabTargetReader,
)
from app.modules.integrations.gitlab.infrastructure.webhook_receipts import (
    SqlAlchemyGitLabWebhookReceiptUnitOfWork,
)
from app.modules.integrations.webhooks.application.receive_github_delivery import (
    ReceiveWebhookDelivery,
)
from app.modules.repositories.application.installation_repositories import RepositorySnapshot
from app.modules.repositories.application.onboard_repository import load_default_rule_sets
from app.modules.repositories.application.sync_installation_repositories import (
    RepositoryOnboardingInput,
)
from app.modules.reviews.application.project_gitlab_merge_request import (
    GitLabMergeRequestCommand,
    GitLabMergeRequestHandler,
    ProjectGitLabMergeRequest,
)
from app.modules.reviews.application.try_enqueue_webhook_run import (
    PendingRunMessage,
    RunMessagePublisher,
    RunPublicationKind,
    TryEnqueueWebhookRun,
)
from app.modules.reviews.infrastructure.gitlab_eligibility_candidates import (
    SqlAlchemyGitLabEligibilityCandidateStore,
)
from app.modules.reviews.infrastructure.gitlab_merge_request_projection import (
    SqlAlchemyGitLabMergeRequestProjectionLock,
    SqlAlchemyGitLabMergeRequestProjectionUnitOfWork,
)
from app.modules.reviews.infrastructure.webhook_runs import SqlAlchemyWebhookRunUnitOfWork
from tests.gitlab_http_fakes import Stream

_TOKEN = "synthetic-authority-PAT-canary"

_ORIGIN = "https://gitlab.com"


@pytest.fixture
def authority_database() -> Iterator[tuple[str, str, UUID]]:
    database_url = os.environ.get("TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("set TEST_DATABASE_URL to run PostgreSQL integration tests")
    schema = f"test_gitlab_projection_{uuid4().hex}"
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
        pool_size=1,
        max_overflow=0,
        pool_timeout=1,
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


NOW = datetime(2026, 10, 19, 12, tzinfo=UTC)


def command_for(
    target: BoundGitLabTarget, *, iid: int = 12, external_id: int = 901, action: str = "open"
) -> GitLabMergeRequestCommand:
    return GitLabMergeRequestCommand(
        target.origin_provider_installation_id, target.project_id, external_id, iid, action
    )


def canonical(
    target: BoundGitLabTarget,
    *,
    head: str = "a" * 40,
    state: str = "opened",
    iid: int = 12,
    external_id: int = 901,
) -> dict[str, Any]:
    return {
        "id": external_id,
        "iid": iid,
        "project_id": target.project_id,
        "target_project_id": target.project_id,
        "source_project_id": 29,
        "state": state,
        "title": "Canonical title",
        "description": "Canonical description",
        "author": {"username": "canonical-author"},
        "web_url": f"{target.instance_origin}/team/project/-/merge_requests/{iid}",
        "source_branch": "feature",
        "target_branch": "main",
        "updated_at": NOW.isoformat(),
        "diff_refs": {"head_sha": head, "start_sha": "b" * 40, "base_sha": "c" * 40},
    }


def response(data: dict[str, Any]) -> httpx.Response:
    return httpx.Response(
        200,
        headers={"Content-Type": "application/json"},
        stream=Stream((json.dumps(data).encode(),)),
    )


def project(
    fixture: Rig,
    client: httpx.AsyncClient,
    lock: SqlAlchemyGitLabMergeRequestProjectionLock,
    *,
    now: Callable[[], datetime] = lambda: NOW,
) -> ProjectGitLabMergeRequest:
    return ProjectGitLabMergeRequest(
        authority=fixture.reader,
        current_provider=HttpGitLabCurrentMergeRequestProvider(
            client=client,
            authority=fixture.authority,
            origin_policy=GitLabOriginPolicy(frozenset({_ORIGIN, "https://other.gitlab.example"})),
        ),
        projection_lock=lock,
        uow_factory=lambda: SqlAlchemyGitLabMergeRequestProjectionUnitOfWork(fixture.sessions),
        now=now,
    )


@asynccontextmanager
async def lock_for(
    database: tuple[str, str, UUID], *, timeout: float = 1
) -> AsyncIterator[SqlAlchemyGitLabMergeRequestProjectionLock]:
    url, schema, _ = database
    lock = SqlAlchemyGitLabMergeRequestProjectionLock(
        url,
        connect_args={"options": f"-csearch_path={schema}", "application_name": "t8c-lock"},
        lock_timeout_seconds=timeout,
    )
    try:
        yield lock
    finally:
        await lock.aclose()


async def assert_no_sql_transaction(url: str) -> None:
    observer = create_async_engine(url, poolclass=NullPool)
    try:
        async with observer.connect() as connection:
            rows = (
                await connection.execute(
                    text(
                        "SELECT application_name,xact_start "
                        "FROM pg_stat_activity WHERE datname=current_database() "
                        "AND pid<>pg_backend_pid() AND backend_type='client backend'"
                    )
                )
            ).all()
            assert all(row.xact_start is None for row in rows), rows
            assert any(row.application_name == "t8c-lock" for row in rows)
    finally:
        await observer.dispose()


async def seed_run(
    fixture: Rig,
    target: BoundGitLabTarget,
    change_id: UUID,
    *,
    state: str = "queued",
    attempt: int = 0,
    head: str = "a" * 40,
    origin: UUID | None = None,
) -> UUID:
    run = uuid4()
    async with fixture.sessions.begin() as session:
        rule = await session.scalar(
            text("SELECT id FROM rule_versions WHERE repository_id=:id AND is_active"),
            {"id": target.repository_id},
        )
        prompt = await session.scalar(text("SELECT id FROM prompt_versions WHERE is_active"))
        await session.execute(
            text(
                "INSERT INTO runs (id,code_change_id,origin_provider_installation_id,"
                "base_sha,head_sha,state,trigger,idempotency_key,engine,rule_version_id,prompt_ve"
                "rsion_id,"
                "attempt,available_at,error_code,finished_at) VALUES "
                "(:id,:change,:origin,:base,:head,"
                "CAST(:state AS run_state),'manual',:key,'fast',:rule,:prompt,:attempt,:now,"
                "CASE WHEN :terminal THEN 'history' ELSE NULL END,CASE WHEN :terminal THEN :now "
                "ELSE NULL END)"
            ),
            {
                "id": run,
                "change": change_id,
                "origin": origin or target.origin_provider_installation_id,
                "base": "b" * 40,
                "head": head,
                "state": state,
                "key": uuid4().hex * 2,
                "rule": rule,
                "prompt": prompt,
                "attempt": attempt,
                "now": NOW,
                "terminal": state in {"succeeded", "failed", "cancelled", "skipped"},
            },
        )
    return run


async def run_json(fixture: Rig, run: UUID) -> object:
    async with fixture.sessions() as session:
        return await session.scalar(
            text("SELECT row_to_json(r)::text FROM runs r WHERE id=:id"), {"id": run}
        )


@dataclass
class Clock:
    value: datetime = NOW

    def __call__(self) -> datetime:
        return self.value


def receiver(
    fixture: Rig, handler: GitLabMergeRequestHandler, clock: Clock
) -> ReceiveWebhookDelivery:
    return ReceiveWebhookDelivery(
        provider="gitlab",
        uow_factory=lambda: SqlAlchemyGitLabWebhookReceiptUnitOfWork(fixture.sessions),
        dispatcher=GitLabWebhookDispatchAdapter(handler),
        now=clock,
        action_of=action_of,
    )


def receipt_for(target: BoundGitLabTarget, key: str = "stable-id", action: str = "open") -> Any:
    return prepare_gitlab_receipt(
        connection_id=target.origin_provider_installation_id,
        event_name="Merge Request Hook",
        webhook_id=key,
        payload_json=json.dumps(
            {
                "object_kind": "merge_request",
                "project": {"id": 17},
                "object_attributes": {
                    "id": 901,
                    "iid": 12,
                    "action": action,
                    "title": "untrusted title",
                    "head_sha": "wrong",
                    "state": "wrong",
                    "url": "https://evil.invalid",
                },
                "workspace_id": str(uuid4()),
                "installation": {"id": 999},
            }
        ),
    )


def automatic_response(request: httpx.Request, data: dict[str, Any]) -> httpx.Response:
    # Reject GitHub, CI and unmodelled HTTP at the external boundary.
    assert request.method == "GET"
    assert request.url.scheme == "https" and request.url.host == "gitlab.com"
    assert request.url.path == "/api/v4/projects/17/merge_requests/12"
    return response(data)


@dataclass
class ConfirmedPointers:
    messages: list[PendingRunMessage]

    async def publish_confirmed(
        self,
        message: PendingRunMessage,
        *,
        kind: RunPublicationKind = RunPublicationKind.QUEUED,
    ) -> None:
        self.messages.append(message)


def automatic_enqueuer(fixture: Rig, publisher: RunMessagePublisher) -> TryEnqueueWebhookRun:
    return TryEnqueueWebhookRun(
        eligibility=DetermineGitLabEligibility(
            candidates=SqlAlchemyGitLabEligibilityCandidateStore(fixture.sessions),
        ),
        uow_factory=lambda: SqlAlchemyWebhookRunUnitOfWork(fixture.sessions),
        publisher=publisher,
        now=lambda: NOW,
    )
