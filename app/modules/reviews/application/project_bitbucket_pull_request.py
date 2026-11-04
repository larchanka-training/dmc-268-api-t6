"""Project canonical Bitbucket PR state and trigger a run for each new head."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID

from app.common.application.unit_of_work import UnitOfWork
from app.modules.integrations.bitbucket.application.provider_error import (
    BitbucketPullRequestResponseError,
)
from app.modules.integrations.bitbucket.application.request_credentials import (
    BoundBitbucketTarget,
)
from app.modules.reviews.application.determine_ci_eligibility import WebhookRunIdentity
from app.modules.reviews.application.try_enqueue_webhook_run import EnqueueResult, EnqueueStatus

_LOGGER = logging.getLogger(__name__)
BITBUCKET_PUSH_BATCH_SIZE = 5


class BitbucketPullRequestIdentityMismatch(ValueError):
    def __init__(self) -> None:
        super().__init__("bitbucket_pull_request_identity_mismatch")


class BitbucketPullRequestStale(ValueError):
    def __init__(self) -> None:
        super().__init__("bitbucket_pull_request_stale")


class BitbucketPullRequestUntrackedClosed(ValueError):
    def __init__(self) -> None:
        super().__init__("bitbucket_pull_request_untracked_closed")


class BitbucketPushContinuationPending(Exception):
    """A completed push page was checkpointed and another worker pass is needed."""


class BitbucketPushRetryPending(Exception):
    """All candidates were visited, but a run admission still needs a later retry."""


class BitbucketPushCandidatesFailed(Exception):
    """A PR exhausted bounded retries; mark the receipt failed with its candidate reason."""


@dataclass(frozen=True)
class BitbucketPullRequestCommand:
    installation_id: UUID
    repository_id: UUID
    repository_uuid: UUID
    number: int
    binding_generation: int
    webhook_generation: int

    def __post_init__(self) -> None:
        if (
            any(
                type(value) is not UUID
                for value in (self.installation_id, self.repository_id, self.repository_uuid)
            )
            or type(self.number) is not int
            or not 0 < self.number <= 2147483647
            or type(self.binding_generation) is not int
            or self.binding_generation <= 0
            or type(self.webhook_generation) is not int
            or self.webhook_generation <= 0
        ):
            raise ValueError("Invalid Bitbucket PR command")


@dataclass(frozen=True)
class CanonicalBitbucketPullRequest:
    repository_uuid: UUID
    number: int
    state: str
    title: str
    description: str | None
    author_login: str | None
    source_branch: str
    target_branch: str
    base_sha: str
    head_sha: str | None
    provider_updated_at: datetime
    web_url: str


@dataclass(frozen=True)
class ProjectedBitbucketPullRequest:
    code_change_id: UUID
    repository_external_id: int
    head_sha: str
    state: str
    head_changed: bool
    run_missing: bool = False


@dataclass(frozen=True)
class BitbucketProjectionOutcome:
    projection: ProjectedBitbucketPullRequest
    enqueue: EnqueueResult | None


class BitbucketProjectionAuthority(Protocol):
    async def resolve(self, command: BitbucketPullRequestCommand) -> BoundBitbucketTarget: ...

    async def checkpoint(self, target: BoundBitbucketTarget, webhook_generation: int) -> None: ...


class BitbucketCurrentPullRequest(Protocol):
    async def get_current(
        self, target: BoundBitbucketTarget, number: int
    ) -> CanonicalBitbucketPullRequest: ...


class BitbucketProjectionStore(Protocol):
    async def project(
        self,
        target: BoundBitbucketTarget,
        webhook_generation: int,
        current: CanonicalBitbucketPullRequest,
    ) -> ProjectedBitbucketPullRequest: ...


class BitbucketPushCandidateReader(Protocol):
    async def matching_open_pr_page(
        self,
        installation_id: UUID,
        repository_id: UUID,
        repository_uuid: UUID,
        binding_generation: int,
        webhook_generation: int,
        branches: frozenset[str],
        after_number: int | None,
    ) -> tuple[int, ...]: ...

    async def matching_open_pr_numbers(
        self,
        installation_id: UUID,
        repository_id: UUID,
        repository_uuid: UUID,
        binding_generation: int,
        webhook_generation: int,
        branches: frozenset[str],
    ) -> tuple[int, ...]: ...


class BitbucketIndexRefresh(Protocol):
    async def execute(
        self,
        installation_id: UUID,
        repository_id: UUID,
        repository_uuid: UUID,
        branches: frozenset[str],
        binding_generation: int,
        webhook_generation: int,
    ) -> bool: ...


class BitbucketProjectionUnitOfWork(UnitOfWork, Protocol):
    async def lock_target(self, target: BoundBitbucketTarget, webhook_generation: int) -> None: ...

    @property
    def pull_requests(self) -> BitbucketProjectionStore: ...

    @property
    def push_continuation(self) -> BitbucketPushContinuationStore: ...


class BitbucketPushContinuationStore(Protocol):
    async def position(
        self,
        delivery_id: str,
        repository_id: UUID,
        binding_generation: int,
        webhook_generation: int,
    ) -> int | None: ...

    async def advance(
        self,
        delivery_id: str,
        repository_id: UUID,
        binding_generation: int,
        webhook_generation: int,
        expected: int | None,
        next_number: int,
        deferred_numbers: tuple[tuple[int, str], ...],
    ) -> None: ...

    async def pending_numbers(
        self,
        delivery_id: str,
        repository_id: UUID,
        binding_generation: int,
        webhook_generation: int,
    ) -> tuple[int, ...]: ...

    async def has_pending(
        self,
        delivery_id: str,
        repository_id: UUID,
        binding_generation: int,
        webhook_generation: int,
    ) -> bool: ...

    async def has_failed(
        self,
        delivery_id: str,
        repository_id: UUID,
        binding_generation: int,
        webhook_generation: int,
    ) -> bool: ...

    async def settle_pending(
        self,
        delivery_id: str,
        repository_id: UUID,
        binding_generation: int,
        webhook_generation: int,
        completed: tuple[int, ...],
        deferred: tuple[tuple[int, str], ...],
    ) -> tuple[bool, bool, bool]: ...


class BitbucketRunEnqueuer(Protocol):
    async def execute(
        self,
        code_change_id: UUID,
        expected_head_sha: str,
        *,
        expected_identity: WebhookRunIdentity,
    ) -> EnqueueResult: ...


class ProjectBitbucketPullRequest:
    """Read the current Cloud PR outside a DB transaction, then commit its projection."""

    def __init__(
        self,
        *,
        authority: BitbucketProjectionAuthority,
        current: BitbucketCurrentPullRequest,
        uow_factory: Callable[[], BitbucketProjectionUnitOfWork],
        push_candidates: BitbucketPushCandidateReader,
        enqueuer: BitbucketRunEnqueuer,
        index_refresh: BitbucketIndexRefresh | None = None,
    ) -> None:
        self._authority = authority
        self._current = current
        self._uow_factory = uow_factory
        self._push_candidates = push_candidates
        self._enqueuer = enqueuer
        self._index_refresh = index_refresh

    async def execute(self, command: BitbucketPullRequestCommand) -> BitbucketProjectionOutcome:
        target = await self._authority.resolve(command)
        current = await self._current.get_current(target, command.number)
        if (
            target.installation_id != command.installation_id
            or target.repository_id != command.repository_id
            or target.repository_uuid != command.repository_uuid
            or target.binding_generation != command.binding_generation
            or current.repository_uuid != command.repository_uuid
            or current.number != command.number
        ):
            raise BitbucketPullRequestIdentityMismatch()
        await self._authority.checkpoint(target, command.webhook_generation)
        async with self._uow_factory() as uow:
            await uow.lock_target(target, command.webhook_generation)
            projected = await uow.pull_requests.project(target, command.webhook_generation, current)
            await uow.commit()
        if projected.state != "open" or not (projected.head_changed or projected.run_missing):
            return BitbucketProjectionOutcome(projected, None)
        enqueue = await self._enqueuer.execute(
            projected.code_change_id,
            projected.head_sha,
            expected_identity=WebhookRunIdentity(
                "bitbucket",
                target.installation_id,
                target.workspace_id,
                target.repository_id,
                projected.repository_external_id,
                credential_generation=target.credential_generation,
                repository_uuid=target.repository_uuid,
                binding_generation=target.binding_generation,
                webhook_generation=command.webhook_generation,
            ),
        )
        return BitbucketProjectionOutcome(projected, enqueue)

    async def pull_request(
        self,
        installation_id: UUID,
        repository_id: UUID,
        repository_uuid: UUID,
        number: int,
        binding_generation: int,
        webhook_generation: int,
    ) -> EnqueueResult | None:
        outcome = await self.execute(
            BitbucketPullRequestCommand(
                installation_id,
                repository_id,
                repository_uuid,
                number,
                binding_generation,
                webhook_generation,
            )
        )
        return outcome.enqueue

    async def push(
        self,
        installation_id: UUID,
        repository_id: UUID,
        repository_uuid: UUID,
        branches: frozenset[str],
        binding_generation: int,
        webhook_generation: int,
        delivery_id: str | None = None,
    ) -> tuple[EnqueueResult, ...]:
        after_number: int | None = None
        if delivery_id is not None:
            async with self._uow_factory() as uow:
                after_number = await uow.push_continuation.position(
                    delivery_id, repository_id, binding_generation, webhook_generation
                )
        if self._index_refresh is not None and after_number is None:
            try:
                await self._index_refresh.execute(
                    installation_id,
                    repository_id,
                    repository_uuid,
                    branches,
                    binding_generation,
                    webhook_generation,
                )
            except Exception:
                _LOGGER.exception(
                    "Bitbucket index refresh failed for repository %s; continuing PR reevaluation",
                    repository_id,
                )
        if delivery_id is None:
            numbers = await self._push_candidates.matching_open_pr_numbers(
                installation_id,
                repository_id,
                repository_uuid,
                binding_generation,
                webhook_generation,
                branches,
            )
            legacy_outcomes: list[EnqueueResult] = []
            for number in numbers:
                outcome = await self.pull_request(
                    installation_id,
                    repository_id,
                    repository_uuid,
                    number,
                    binding_generation,
                    webhook_generation,
                )
                if outcome is not None:
                    legacy_outcomes.append(outcome)
            return tuple(legacy_outcomes)

        numbers = await self._push_candidates.matching_open_pr_page(
            installation_id,
            repository_id,
            repository_uuid,
            binding_generation,
            webhook_generation,
            branches,
            after_number,
        )
        if not numbers:
            async with self._uow_factory() as uow:
                pending = await uow.push_continuation.pending_numbers(
                    delivery_id, repository_id, binding_generation, webhook_generation
                )
            if not pending:
                async with self._uow_factory() as uow:
                    waiting = await uow.push_continuation.has_pending(
                        delivery_id, repository_id, binding_generation, webhook_generation
                    )
                    failed = await uow.push_continuation.has_failed(
                        delivery_id, repository_id, binding_generation, webhook_generation
                    )
                if waiting:
                    raise BitbucketPushRetryPending()
                if failed:
                    raise BitbucketPushCandidatesFailed()
                return ()
            completed: list[int] = []
            deferred: list[tuple[int, str]] = []
            retry_outcomes: list[EnqueueResult] = []
            for number in pending:
                try:
                    outcome = await self.pull_request(
                        installation_id,
                        repository_id,
                        repository_uuid,
                        number,
                        binding_generation,
                        webhook_generation,
                    )
                except BitbucketPullRequestStale:
                    deferred.append((number, "canonical_stale"))
                    continue
                except BitbucketPullRequestResponseError as error:
                    deferred.append((number, error.code))
                    continue
                if outcome is not None:
                    retry_outcomes.append(outcome)
                if outcome is not None and outcome.status == EnqueueStatus.DEFERRED:
                    deferred.append((number, "run_admission_deferred"))
                elif outcome is not None and outcome.status == EnqueueStatus.STALE:
                    deferred.append((number, "run_admission_stale"))
                else:
                    completed.append(number)
            target = await self._authority.resolve(
                BitbucketPullRequestCommand(
                    installation_id,
                    repository_id,
                    repository_uuid,
                    pending[-1],
                    binding_generation,
                    webhook_generation,
                )
            )
            async with self._uow_factory() as uow:
                await uow.lock_target(target, webhook_generation)
                remaining, untried, failed = await uow.push_continuation.settle_pending(
                    delivery_id,
                    repository_id,
                    binding_generation,
                    webhook_generation,
                    tuple(completed),
                    tuple(deferred),
                )
                await uow.commit()
            if remaining:
                if untried or completed:
                    raise BitbucketPushContinuationPending()
                raise BitbucketPushRetryPending()
            if failed:
                raise BitbucketPushCandidatesFailed()
            return tuple(retry_outcomes)
        page_outcomes: list[EnqueueResult] = []
        deferred_numbers: list[tuple[int, str]] = []
        for number in numbers:
            try:
                outcome = await self.pull_request(
                    installation_id,
                    repository_id,
                    repository_uuid,
                    number,
                    binding_generation,
                    webhook_generation,
                )
            except BitbucketPullRequestStale:
                deferred_numbers.append((number, "canonical_stale"))
                continue
            except BitbucketPullRequestResponseError as error:
                deferred_numbers.append((number, error.code))
                continue
            if outcome is not None:
                page_outcomes.append(outcome)
                if outcome.status == EnqueueStatus.DEFERRED:
                    deferred_numbers.append((number, "run_admission_deferred"))
                elif outcome.status == EnqueueStatus.STALE:
                    deferred_numbers.append((number, "run_admission_stale"))
        # Remote reads and run admissions have ended. Persist only a completed
        # page; replay of a partial page uses PR/head run uniqueness.
        target = await self._authority.resolve(
            BitbucketPullRequestCommand(
                installation_id,
                repository_id,
                repository_uuid,
                numbers[-1],
                binding_generation,
                webhook_generation,
            )
        )
        async with self._uow_factory() as uow:
            await uow.lock_target(target, webhook_generation)
            await uow.push_continuation.advance(
                delivery_id,
                repository_id,
                binding_generation,
                webhook_generation,
                after_number,
                numbers[-1],
                tuple(deferred_numbers),
            )
            await uow.commit()
        if len(numbers) == BITBUCKET_PUSH_BATCH_SIZE:
            raise BitbucketPushContinuationPending()
        async with self._uow_factory() as uow:
            pending = await uow.push_continuation.pending_numbers(
                delivery_id, repository_id, binding_generation, webhook_generation
            )
            waiting = await uow.push_continuation.has_pending(
                delivery_id, repository_id, binding_generation, webhook_generation
            )
            failed = await uow.push_continuation.has_failed(
                delivery_id, repository_id, binding_generation, webhook_generation
            )
        if pending:
            raise BitbucketPushContinuationPending()
        if waiting:
            raise BitbucketPushRetryPending()
        if failed:
            raise BitbucketPushCandidatesFailed()
        return tuple(page_outcomes)
