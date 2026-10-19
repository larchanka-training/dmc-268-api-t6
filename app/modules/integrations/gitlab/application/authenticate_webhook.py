"""Digest-only GitLab webhook authentication through detached application ports."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol
from uuid import UUID


@dataclass(frozen=True)
class GitLabWebhookAuthRecord:
    connection_id: UUID
    webhook_secret_digest: str = field(repr=False)
    active: bool
    intake_enabled: bool


class GitLabWebhookAuthReader(Protocol):
    async def read(self, connection_id: UUID) -> GitLabWebhookAuthRecord | None: ...


class GitLabWebhookSecretVerifier(Protocol):
    def verify(self, secret: str | None, expected_digest: str) -> bool: ...


class GitLabWebhookAuthStatus(StrEnum):
    AUTHENTICATED = "authenticated"
    INVALID_CREDENTIALS = "invalid_credentials"
    DISABLED = "disabled"


class AuthenticateGitLabWebhook:
    def __init__(
        self, *, reader: GitLabWebhookAuthReader, verifier: GitLabWebhookSecretVerifier
    ) -> None:
        self._reader = reader
        self._verifier = verifier

    async def execute(self, *, connection_id: UUID, secret: str | None) -> GitLabWebhookAuthStatus:
        record = await self._reader.read(connection_id)
        if (
            record is None
            or not self._verifier.verify(secret, record.webhook_secret_digest)
            or record.connection_id != connection_id
            or not record.active
        ):
            return GitLabWebhookAuthStatus.INVALID_CREDENTIALS
        if not record.intake_enabled:
            return GitLabWebhookAuthStatus.DISABLED
        return GitLabWebhookAuthStatus.AUTHENTICATED
