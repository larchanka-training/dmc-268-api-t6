"""Detached persisted-run admission using the sole GitLab request authority."""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable
from typing import Literal, Protocol
from uuid import UUID

from sqlalchemy.exc import DisconnectionError, InterfaceError, OperationalError
from sqlalchemy.exc import TimeoutError as SqlAlchemyTimeoutError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.modules.integrations.gitlab.application.request_credentials import (
    BoundGitLabTarget,
    GitLabCredentialGenerationChanged,
    GitLabCredentialUnavailable,
    GitLabRequestAuthority,
    GitLabRequestCredential,
    GitLabRequestUnavailable,
    GitLabTargetIdentity,
    GitLabTargetInactive,
)
from app.modules.reviews.application.check_runs import RunProviderCheckpoint
from app.modules.reviews.application.process_run import RunVcsInput
from app.modules.reviews.application.run_failures import RequiredSourceUnavailable, RunCancelled
from app.modules.reviews.infrastructure.run_repository import SqlAlchemyRunRepository

type FetchCode = Literal["diff_fetch_failed", "commit_messages_fetch_failed"]


class GitLabRunCredentialPort(Protocol):
    @property
    def expected_input(self) -> RunVcsInput: ...

    async def bind(self, *, error_code: FetchCode = "diff_fetch_failed") -> BoundGitLabTarget: ...

    async def credential_for_request(
        self, *, error_code: FetchCode = "diff_fetch_failed"
    ) -> GitLabRequestCredential: ...

    async def checkpoint(self, *, error_code: FetchCode = "diff_fetch_failed") -> None: ...


class GitLabRunCredentials:
    """One attempt's complete route and generation, never a plaintext-token cache.

    The trusted worker supplies expected_workspace_id from its claimed attempt. The
    existing detached VCS reader does not return workspace or attempt identity.
    """

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        authority: GitLabRequestAuthority,
        run_id: UUID,
        expected_workspace_id: UUID,
        expected_input: RunVcsInput,
        checkpoint: RunProviderCheckpoint,
    ) -> None:
        if (
            type(run_id) is not UUID
            or type(expected_workspace_id) is not UUID
            or not isinstance(expected_input, RunVcsInput)
        ):
            raise ValueError("Invalid GitLab run identity")
        locator = expected_input.locator
        if (
            locator.provider != "gitlab"
            or type(expected_input.repository_id) is not UUID
            or type(expected_input.code_change_id) is not UUID
            or type(locator.origin_provider_installation_id) is not UUID
            or any(
                type(value) is not int or not 0 < value <= 9223372036854775807
                for value in (
                    locator.repository_external_id,
                    locator.installation_external_id,
                    locator.number,
                )
            )
            or not isinstance(locator.repository_full_name, str)
            or not 1 <= len(locator.repository_full_name) <= 1024
            or any(ord(char) < 32 for char in locator.repository_full_name)
            or any(
                not isinstance(value, str)
                or re.fullmatch(r"(?:[a-fA-F0-9]{40}|[a-fA-F0-9]{64})", value) is None
                for value in (expected_input.head_sha, expected_input.base_sha)
            )
        ):
            raise ValueError("Invalid GitLab run input")
        self._repository = SqlAlchemyRunRepository(session_factory, allow_unscoped=True)
        self._authority = authority
        self._run_id = run_id
        self._workspace_id = expected_workspace_id
        self._expected_input = expected_input
        self._attempt_checkpoint = checkpoint
        self._target: BoundGitLabTarget | None = None
        self._lock = asyncio.Lock()

    @property
    def expected_input(self) -> RunVcsInput:
        return self._expected_input

    async def _operation[T](self, operation: Awaitable[T], error_code: FetchCode) -> T:
        try:
            return await operation
        except (GitLabTargetInactive, GitLabCredentialGenerationChanged):
            raise RunCancelled("installation_unavailable") from None
        except (
            GitLabCredentialUnavailable,
            GitLabRequestUnavailable,
            OperationalError,
            InterfaceError,
            DisconnectionError,
            SqlAlchemyTimeoutError,
            OSError,
        ):
            # All admission stages, including the caller's SQL checkpoint, share this
            # narrow operational boundary. Mapping/programmer/control faults escape.
            raise RequiredSourceUnavailable(error_code) from None

    async def _admit(self, error_code: FetchCode) -> BoundGitLabTarget:
        await self._operation(self._attempt_checkpoint(), error_code)
        current = await self._operation(
            self._repository.get_run_vcs_input(self._run_id), error_code
        )
        if current != self._expected_input:
            raise RunCancelled("installation_unavailable")
        if self._target is None:
            locator = self._expected_input.locator
            assert locator.origin_provider_installation_id is not None
            assert locator.repository_external_id is not None
            target = await self._operation(
                self._authority.resolve_target(
                    GitLabTargetIdentity(
                        locator.origin_provider_installation_id, locator.repository_external_id
                    )
                ),
                error_code,
            )
            if (
                target.workspace_id != self._workspace_id
                or target.repository_id != current.repository_id
                or target.origin_provider_installation_id != locator.origin_provider_installation_id
                or target.project_id != locator.repository_external_id
            ):
                raise RunCancelled("installation_unavailable")
            self._target = target
        else:
            await self._operation(self._authority.checkpoint(self._target), error_code)
        return self._target

    @staticmethod
    def _validate_code(error_code: FetchCode) -> None:
        if error_code not in {"diff_fetch_failed", "commit_messages_fetch_failed"}:
            raise ValueError("Invalid GitLab source failure code")

    async def bind(self, *, error_code: FetchCode = "diff_fetch_failed") -> BoundGitLabTarget:
        self._validate_code(error_code)
        async with self._lock:
            target = await self._admit(error_code)
            await self._operation(self._attempt_checkpoint(), error_code)
            return target

    async def credential_for_request(
        self, *, error_code: FetchCode = "diff_fetch_failed"
    ) -> GitLabRequestCredential:
        self._validate_code(error_code)
        async with self._lock:
            target = await self._admit(error_code)
            credential = await self._operation(self._authority.credential_for(target), error_code)
            if credential.target != target:
                raise RunCancelled("installation_unavailable")
            await self._operation(self._attempt_checkpoint(), error_code)
            return credential

    async def checkpoint(self, *, error_code: FetchCode = "diff_fetch_failed") -> None:
        await self.bind(error_code=error_code)
