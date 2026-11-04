"""Shared Bitbucket projection test doubles and PostgreSQL setup."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.integrations.bitbucket.application.request_credentials import BoundBitbucketTarget
from app.modules.integrations.bitbucket.infrastructure.connection_store import (
    SqlAlchemyBitbucketConnectionStore,
)
from app.modules.integrations.bitbucket.infrastructure.credential_cipher import (
    BitbucketCredentialCipher,
    CredentialContext,
    CredentialPurpose,
)
from app.modules.reviews.application.determine_ci_eligibility import WebhookRunIdentity
from app.modules.reviews.application.project_bitbucket_pull_request import (
    CanonicalBitbucketPullRequest,
)
from app.modules.reviews.application.try_enqueue_webhook_run import (
    EnqueueResult,
    EnqueueStatus,
    PendingRunMessage,
    RunPublicationKind,
)

NOW = datetime(2026, 10, 26, 12, tzinfo=UTC)


async def seed_confirmed_bitbucket_repository(
    session: AsyncSession,
    *,
    workspace: UUID,
    installation: UUID,
    remote_workspace: UUID,
    remote_repo: UUID,
    cipher: BitbucketCredentialCipher,
) -> UUID:
    store = SqlAlchemyBitbucketConnectionStore(session)
    await store.create_connection(
        workspace_id=workspace,
        installation_id=installation,
        bitbucket_workspace_uuid=remote_workspace,
        token=cipher.encrypt(
            "test-token",
            context=CredentialContext(installation, workspace),
            purpose=CredentialPurpose.TOKEN,
        ),
    )
    repository, _ = await store.bind_repository(
        workspace_id=workspace,
        installation_id=installation,
        repository_uuid=remote_repo,
        full_name="team/repo",
        default_branch="main",
        web_url="https://bitbucket.org/team/repo",
    )
    await store.set_repository_webhook_secret(
        workspace_id=workspace,
        installation_id=installation,
        repository_id=repository,
        secret=cipher.encrypt(
            "s" * 40,
            context=CredentialContext(installation, workspace, repository),
            purpose=CredentialPurpose.WEBHOOK_SECRET,
        ),
    )
    await session.execute(
        text(
            "UPDATE bitbucket_repository_webhook_secrets SET confirmed_at=now() "
            "WHERE repository_id=:repository"
        ),
        {"repository": repository},
    )
    return repository


@dataclass
class Current:
    head: str = "a" * 40
    updated_at: datetime = NOW
    title: str = "First"

    async def get_current(
        self, target: BoundBitbucketTarget, number: int
    ) -> CanonicalBitbucketPullRequest:
        return CanonicalBitbucketPullRequest(
            target.repository_uuid,
            number,
            "open",
            self.title,
            None,
            "reviewer",
            "feature",
            "main",
            "b" * 40,
            self.head,
            self.updated_at,
            f"https://bitbucket.org/team/repo/pull-requests/{number}",
        )


@dataclass
class Enqueuer:
    heads: list[str] = field(default_factory=list)

    async def execute(
        self, code_change_id: UUID, expected_head_sha: str, *, expected_identity: WebhookRunIdentity
    ) -> EnqueueResult:
        assert expected_identity.provider == "bitbucket"
        self.heads.append(expected_head_sha)
        return EnqueueResult(EnqueueStatus.ENQUEUED, uuid4())


@dataclass
class Publisher:
    messages: list[PendingRunMessage] = field(default_factory=list)
    fail_heads: set[str] = field(default_factory=set)

    async def publish_confirmed(
        self, message: PendingRunMessage, *, kind: RunPublicationKind = RunPublicationKind.QUEUED
    ) -> None:
        assert message.provider == "bitbucket" and kind == RunPublicationKind.QUEUED
        self.messages.append(message)
        if message.head_sha in self.fail_heads:
            raise RuntimeError("synthetic publisher failure")


class FailingEnqueuer:
    async def execute(
        self, code_change_id: UUID, expected_head_sha: str, *, expected_identity: WebhookRunIdentity
    ) -> EnqueueResult:
        raise RuntimeError("synthetic crash after PR projection")
