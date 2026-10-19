"""Origin-pinned request authority, independent of SQL, HTTP and key configuration."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol
from uuid import UUID

from app.modules.integrations.gitlab.application.manage_connection import (
    CredentialEnvelope,
    GitLabCredentialCodec,
    GitLabManagementError,
    GitLabOriginPolicy,
)


class GitLabTargetInactive(ValueError):
    """The immutable origin/binding no longer authorizes this target."""

    def __init__(self) -> None:
        super().__init__("gitlab_target_inactive")


class GitLabCredentialGenerationChanged(ValueError):
    """Stop this attempt; a later bounded attempt may resolve a new generation."""

    def __init__(self) -> None:
        super().__init__("gitlab_credential_generation_changed")


class GitLabCredentialUnavailable(ValueError):
    """The configured codec could not authenticate the stored credential."""

    def __init__(self) -> None:
        super().__init__("gitlab_credential_unavailable")


class GitLabRequestUnavailable(RuntimeError):
    """Operational failure, distinct from permanent authority refusal."""

    def __init__(self) -> None:
        super().__init__("gitlab_request_unavailable")


@dataclass(frozen=True)
class GitLabTargetIdentity:
    origin_provider_installation_id: UUID
    project_id: int

    def __post_init__(self) -> None:
        if type(self.origin_provider_installation_id) is not UUID or (
            type(self.project_id) is not int or not 0 < self.project_id <= 9223372036854775807
        ):
            raise ValueError("Invalid GitLab target identity")


@dataclass(frozen=True)
class BoundGitLabTarget:
    origin_provider_installation_id: UUID
    workspace_id: UUID
    repository_id: UUID
    instance_origin: str
    project_id: int
    credential_generation: int

    def __post_init__(self) -> None:
        GitLabTargetIdentity(self.origin_provider_installation_id, self.project_id)
        if (
            type(self.workspace_id) is not UUID
            or type(self.repository_id) is not UUID
            or type(self.credential_generation) is not int
            or not 0 < self.credential_generation <= 9223372036854775807
        ):
            raise ValueError("Invalid GitLab bound target")
        try:
            # Syntax only. Actual HTTP composition must apply its runtime allowlist/trust policy.
            GitLabOriginPolicy(
                frozenset({self.instance_origin}), frozenset({self.instance_origin})
            ).validate(self.instance_origin)
        except (ValueError, TypeError):
            raise ValueError("Invalid GitLab bound target") from None


@dataclass(frozen=True)
class GitLabRequestCredential:
    """Ephemeral adapter value; never serialize, log or globally cache this object."""

    target: BoundGitLabTarget
    token: str = field(repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.target, BoundGitLabTarget) or (
            not isinstance(self.token, str)
            or not 1 <= len(self.token) <= 4096
            or any(not 33 <= ord(character) <= 126 for character in self.token)
        ):
            raise GitLabCredentialUnavailable()


class GitLabTargetReader(Protocol):
    """Metadata-only capability; needs no codec, keyring, HTTP or CA settings."""

    async def resolve_target(self, identity: GitLabTargetIdentity) -> BoundGitLabTarget: ...

    async def checkpoint(self, target: BoundGitLabTarget) -> None: ...


class GitLabCredentialReader(GitLabTargetReader, Protocol):
    async def credential_envelope_for(self, target: BoundGitLabTarget) -> CredentialEnvelope: ...


class GitLabRequestAuthority(GitLabTargetReader, Protocol):
    async def credential_for(self, target: BoundGitLabTarget) -> GitLabRequestCredential: ...


class GitLabRequestCredentials:
    def __init__(self, reader: GitLabCredentialReader, codec: GitLabCredentialCodec) -> None:
        self._reader = reader
        self._codec = codec

    async def resolve_target(self, identity: GitLabTargetIdentity) -> BoundGitLabTarget:
        return await self._reader.resolve_target(identity)

    async def checkpoint(self, target: BoundGitLabTarget) -> None:
        """Call immediately before each request/retry and before final current-state writes.

        An admitted HTTP request may finish; this does not cancel in-flight wire I/O.
        Final writes also need an atomic authority fence in their own transaction.
        """
        await self._reader.checkpoint(target)

    async def credential_for(self, target: BoundGitLabTarget) -> GitLabRequestCredential:
        credential = await self._reader.credential_envelope_for(target)
        try:
            token = self._codec.decrypt(
                credential,
                installation_id=target.origin_provider_installation_id,
                workspace_id=target.workspace_id,
                instance_origin=target.instance_origin,
            )
        except GitLabManagementError as error:
            if error.code == "credential_operation_failed":
                raise GitLabCredentialUnavailable() from None
            raise GitLabRequestUnavailable() from None
        except Exception:
            raise GitLabRequestUnavailable() from None
        await self._reader.checkpoint(target)
        return GitLabRequestCredential(target, token)
