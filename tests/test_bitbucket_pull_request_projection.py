"""Canonical Bitbucket PR state drives projection before idempotent run admission."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import cast
from uuid import UUID

import pytest

from app.modules.integrations.bitbucket.application.provider_error import (
    BitbucketProviderError,
    BitbucketPullRequestResponseError,
)
from app.modules.integrations.bitbucket.application.request_credentials import (
    BoundBitbucketTarget,
)
from app.modules.reviews.application.determine_ci_eligibility import WebhookRunIdentity
from app.modules.reviews.application.project_bitbucket_pull_request import (
    BitbucketPullRequestCommand,
    BitbucketPushContinuationPending,
    BitbucketPushContinuationStore,
    BitbucketPushRetryPending,
    CanonicalBitbucketPullRequest,
    ProjectBitbucketPullRequest,
    ProjectedBitbucketPullRequest,
)
from app.modules.reviews.application.try_enqueue_webhook_run import EnqueueResult, EnqueueStatus

TARGET = BoundBitbucketTarget(UUID(int=1), UUID(int=2), UUID(int=3), UUID(int=4), 5, 6)
NOW = datetime(2026, 10, 26, tzinfo=UTC)


@dataclass
class Authority:
    calls: list[str]

    async def resolve(self, command: BitbucketPullRequestCommand) -> BoundBitbucketTarget:
        self.calls.append("resolve")
        return TARGET

    async def checkpoint(self, target: BoundBitbucketTarget, webhook_generation: int) -> None:
        assert target == TARGET and webhook_generation == 7
        self.calls.append("checkpoint")


@dataclass
class Current:
    calls: list[str]
    head: str = "a" * 40

    async def get_current(
        self, target: BoundBitbucketTarget, number: int
    ) -> CanonicalBitbucketPullRequest:
        assert target == TARGET and number == 17
        self.calls.append("http")
        return CanonicalBitbucketPullRequest(
            repository_uuid=TARGET.repository_uuid,
            number=17,
            state="open",
            title="Change",
            description=None,
            author_login="reviewer",
            source_branch="feature",
            target_branch="main",
            base_sha="b" * 40,
            head_sha=self.head,
            provider_updated_at=NOW,
            web_url="https://bitbucket.org/team/repo/pull-requests/17",
        )


@dataclass
class Store:
    calls: list[str]
    changed: bool = True

    async def project(
        self,
        target: BoundBitbucketTarget,
        webhook_generation: int,
        current: CanonicalBitbucketPullRequest,
    ) -> ProjectedBitbucketPullRequest:
        assert target == TARGET and webhook_generation == 7
        self.calls.append("project")
        assert current.head_sha is not None
        return ProjectedBitbucketPullRequest(
            UUID(int=9), 17, current.head_sha, current.state, self.changed
        )


@dataclass
class PushCandidates:
    async def matching_open_pr_page(
        self,
        installation_id: UUID,
        repository_id: UUID,
        repository_uuid: UUID,
        binding_generation: int,
        webhook_generation: int,
        branches: frozenset[str],
        after_number: int | None,
    ) -> tuple[int, ...]:
        raise AssertionError("legacy push test must not read a receipt page")

    async def matching_open_pr_numbers(
        self,
        installation_id: UUID,
        repository_id: UUID,
        repository_uuid: UUID,
        binding_generation: int,
        webhook_generation: int,
        branches: frozenset[str],
    ) -> tuple[int, ...]:
        assert (installation_id, repository_id, repository_uuid) == (
            TARGET.installation_id,
            TARGET.repository_id,
            TARGET.repository_uuid,
        )
        assert (binding_generation, webhook_generation) == (6, 7)
        assert branches == frozenset({"feature"})
        return (17,)


@dataclass
class IndexRefresh:
    calls: list[str]

    async def execute(
        self,
        installation_id: UUID,
        repository_id: UUID,
        repository_uuid: UUID,
        branches: frozenset[str],
        binding_generation: int,
        webhook_generation: int,
    ) -> bool:
        assert (installation_id, repository_id, repository_uuid) == (
            TARGET.installation_id,
            TARGET.repository_id,
            TARGET.repository_uuid,
        )
        assert (binding_generation, webhook_generation) == (6, 7)
        assert branches == frozenset({"main"})
        self.calls.append("refresh")
        return True


@dataclass
class FailingIndexRefresh:
    calls: list[str]

    async def execute(
        self,
        installation_id: UUID,
        repository_id: UUID,
        repository_uuid: UUID,
        branches: frozenset[str],
        binding_generation: int,
        webhook_generation: int,
    ) -> bool:
        self.calls.append("refresh")
        raise RuntimeError("index is temporarily unavailable")


@dataclass
class NoCandidates:
    calls: list[str]

    async def matching_open_pr_page(
        self,
        installation_id: UUID,
        repository_id: UUID,
        repository_uuid: UUID,
        binding_generation: int,
        webhook_generation: int,
        branches: frozenset[str],
        after_number: int | None,
    ) -> tuple[int, ...]:
        raise AssertionError("legacy push test must not read a receipt page")

    async def matching_open_pr_numbers(
        self,
        installation_id: UUID,
        repository_id: UUID,
        repository_uuid: UUID,
        binding_generation: int,
        webhook_generation: int,
        branches: frozenset[str],
    ) -> tuple[int, ...]:
        self.calls.append("pr_candidates")
        return ()


def test_default_branch_push_wakes_index_without_open_pull_requests() -> None:
    """The signed push always refreshes RAG, independently of PR run projection."""

    async def scenario() -> None:
        calls: list[str] = []
        use_case = ProjectBitbucketPullRequest(
            authority=Authority(calls),
            current=Current(calls),
            uow_factory=lambda: Uow(calls, Store(calls)),
            push_candidates=NoCandidates(calls),
            enqueuer=Enqueuer(calls),
            index_refresh=IndexRefresh(calls),
        )
        result = await use_case.push(
            TARGET.installation_id,
            TARGET.repository_id,
            TARGET.repository_uuid,
            frozenset({"main"}),
            6,
            7,
        )
        assert result == ()
        assert calls == ["refresh", "pr_candidates"]

    asyncio.run(scenario())


def test_push_rechecks_pull_requests_when_index_refresh_fails(
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def scenario() -> None:
        calls: list[str] = []
        enqueuer = Enqueuer(calls)
        use_case = ProjectBitbucketPullRequest(
            authority=Authority(calls),
            current=Current(calls),
            uow_factory=lambda: Uow(calls, Store(calls)),
            push_candidates=PushCandidates(),
            enqueuer=enqueuer,
            index_refresh=FailingIndexRefresh(calls),
        )
        result = await use_case.push(
            TARGET.installation_id,
            TARGET.repository_id,
            TARGET.repository_uuid,
            frozenset({"feature"}),
            6,
            7,
        )
        assert len(result) == 1 and result[0].status == EnqueueStatus.ENQUEUED
        assert calls == [
            "refresh",
            "resolve",
            "http",
            "checkpoint",
            "lock",
            "project",
            "commit",
            "enqueue",
        ]

    asyncio.run(scenario())
    assert any("Bitbucket index refresh failed" in record.message for record in caplog.records)


@dataclass
class Uow:
    calls: list[str]
    pull_requests: Store

    @property
    def push_continuation(self) -> BitbucketPushContinuationStore:
        raise AssertionError("legacy push test must not read a receipt continuation")

    async def __aenter__(self) -> Uow:
        return self

    async def __aexit__(self, *_: object) -> None:
        pass

    async def lock_target(self, target: BoundBitbucketTarget, webhook_generation: int) -> None:
        assert target == TARGET and webhook_generation == 7
        self.calls.append("lock")

    async def commit(self) -> None:
        self.calls.append("commit")

    async def rollback(self) -> None:
        pass


@dataclass
class Enqueuer:
    calls: list[str]
    identities: list[WebhookRunIdentity] = field(default_factory=list)

    async def execute(
        self, code_change_id: UUID, expected_head_sha: str, *, expected_identity: WebhookRunIdentity
    ) -> EnqueueResult:
        assert code_change_id == UUID(int=9) and expected_head_sha == "a" * 40
        self.calls.append("enqueue")
        self.identities.append(expected_identity)
        return EnqueueResult(EnqueueStatus.ENQUEUED, UUID(int=10))


def test_canonical_read_finishes_before_projection_commit_and_enqueue() -> None:
    async def scenario() -> None:
        calls: list[str] = []
        enqueuer = Enqueuer(calls)
        store = Store(calls)
        use_case = ProjectBitbucketPullRequest(
            authority=Authority(calls),
            current=Current(calls),
            uow_factory=lambda: Uow(calls, store),
            push_candidates=PushCandidates(),
            enqueuer=enqueuer,
        )
        outcome = await use_case.execute(
            BitbucketPullRequestCommand(
                TARGET.installation_id,
                TARGET.repository_id,
                TARGET.repository_uuid,
                17,
                6,
                7,
            )
        )
        assert calls == ["resolve", "http", "checkpoint", "lock", "project", "commit", "enqueue"]
        assert outcome.enqueue is not None and outcome.enqueue.status == EnqueueStatus.ENQUEUED
        assert enqueuer.identities[0].provider == "bitbucket"
        assert enqueuer.identities[0].binding_generation == 6

    asyncio.run(scenario())


def test_metadata_only_projection_does_not_create_a_new_run() -> None:
    async def scenario() -> None:
        calls: list[str] = []
        store = Store(calls, False)
        use_case = ProjectBitbucketPullRequest(
            authority=Authority(calls),
            current=Current(calls),
            uow_factory=lambda: Uow(calls, store),
            push_candidates=PushCandidates(),
            enqueuer=Enqueuer(calls),
        )
        outcome = await use_case.execute(
            BitbucketPullRequestCommand(
                TARGET.installation_id,
                TARGET.repository_id,
                TARGET.repository_uuid,
                17,
                6,
                7,
            )
        )
        assert calls == ["resolve", "http", "checkpoint", "lock", "project", "commit"]
        assert outcome.enqueue is None

    asyncio.run(scenario())


def test_push_keeps_scanning_after_one_invalid_pull_request() -> None:
    class PageCandidates(PushCandidates):
        async def matching_open_pr_page(
            self,
            installation_id: UUID,
            repository_id: UUID,
            repository_uuid: UUID,
            binding_generation: int,
            webhook_generation: int,
            branches: frozenset[str],
            after_number: int | None,
        ) -> tuple[int, ...]:
            first = 17 if after_number is None else after_number + 1
            return tuple(range(first, min(first + 5, 520)))

    class CurrentWithOneInvalid(Current):
        invalid_19 = True

        async def get_current(
            self, target: BoundBitbucketTarget, number: int
        ) -> CanonicalBitbucketPullRequest:
            self.calls.append(f"http:{number}")
            if number == 19 and self.invalid_19:
                raise BitbucketPullRequestResponseError()
            return CanonicalBitbucketPullRequest(
                repository_uuid=TARGET.repository_uuid,
                number=number,
                state="open",
                title="Change",
                description=None,
                author_login="reviewer",
                source_branch="feature",
                target_branch="main",
                base_sha="b" * 40,
                head_sha="a" * 40,
                provider_updated_at=NOW,
                web_url=f"https://bitbucket.org/team/repo/pull-requests/{number}",
            )

    @dataclass
    class Continuation:
        position_value: int | None = None
        deferred: list[tuple[int, str]] = field(default_factory=list)
        due: bool = True
        untried: bool = False

        async def position(self, *_: object) -> int | None:
            return self.position_value

        async def advance(
            self,
            delivery_id: str,
            repository_id: UUID,
            binding_generation: int,
            webhook_generation: int,
            expected: int | None,
            next_number: int,
            deferred_numbers: tuple[tuple[int, str], ...],
        ) -> None:
            assert self.position_value == expected
            self.position_value = next_number
            self.deferred.extend(deferred_numbers)

        async def pending_numbers(self, *_: object) -> tuple[int, ...]:
            return (19,) if self.due else ()

        async def has_failed(self, *_: object) -> bool:
            return False

        async def has_pending(self, *_: object) -> bool:
            return True

        async def settle_pending(
            self,
            delivery_id: str,
            repository_id: UUID,
            binding_generation: int,
            webhook_generation: int,
            completed: tuple[int, ...],
            deferred: tuple[tuple[int, str], ...],
        ) -> tuple[bool, bool, bool]:
            if self.untried:
                assert completed == ()
                assert deferred == ((19, "invalid_provider_response"),)
            return True, self.untried, False

    class PushUow(Uow):
        def __init__(self, calls: list[str], continuation: Continuation) -> None:
            super().__init__(calls, Store(calls))
            self._continuation = continuation

        @property
        def push_continuation(self) -> BitbucketPushContinuationStore:
            return cast(BitbucketPushContinuationStore, self._continuation)

        async def __aenter__(self) -> PushUow:
            return self

    async def scenario() -> None:
        calls: list[str] = []
        continuation = Continuation()
        current = CurrentWithOneInvalid(calls)
        use_case = ProjectBitbucketPullRequest(
            authority=Authority(calls),
            current=current,
            uow_factory=lambda: PushUow(calls, continuation),
            push_candidates=PageCandidates(),
            enqueuer=Enqueuer(calls),
        )
        args = (
            TARGET.installation_id,
            TARGET.repository_id,
            TARGET.repository_uuid,
            frozenset({"feature"}),
            6,
            7,
            "receipt-1",
        )
        for _ in range(101):
            with pytest.raises(BitbucketPushContinuationPending):
                await use_case.push(*args)
        assert continuation.position_value == 519
        assert continuation.deferred == [(19, "invalid_provider_response")]
        assert "http:519" in calls
        assert sum(call.startswith("http:") for call in calls) == 503
        with pytest.raises(BitbucketPushRetryPending):
            await use_case.push(*args)
        current.invalid_19 = False
        with pytest.raises(BitbucketPushContinuationPending):
            await use_case.push(*args)
        current.invalid_19 = True
        continuation.untried = True
        with pytest.raises(BitbucketPushContinuationPending):
            await use_case.push(*args)
        continuation.due = False
        with pytest.raises(BitbucketPushRetryPending):
            await use_case.push(*args)
        assert continuation.position_value == 519

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "provider_error",
    [
        BitbucketProviderError("provider_rate_limited", retry_after=3600),
        BitbucketProviderError("invalid_provider_response"),
    ],
)
def test_push_shared_provider_failure_replays_only_the_uncommitted_page(
    provider_error: BitbucketProviderError,
) -> None:
    class PageCandidates(PushCandidates):
        async def matching_open_pr_page(
            self,
            installation_id: UUID,
            repository_id: UUID,
            repository_uuid: UUID,
            binding_generation: int,
            webhook_generation: int,
            branches: frozenset[str],
            after_number: int | None,
        ) -> tuple[int, ...]:
            assert after_number is None
            return (17, 19)

    class RateLimitedCurrent(Current):
        async def get_current(
            self, target: BoundBitbucketTarget, number: int
        ) -> CanonicalBitbucketPullRequest:
            if number == 19:
                raise provider_error
            return await super().get_current(target, number)

    class Continuation:
        advances = 0

        async def position(self, *_: object) -> int | None:
            return None

        async def advance(self, *_: object) -> None:
            self.advances += 1

    class PushUow(Uow):
        def __init__(self, calls: list[str], continuation: Continuation) -> None:
            super().__init__(calls, Store(calls))
            self._continuation = continuation

        @property
        def push_continuation(self) -> BitbucketPushContinuationStore:
            return cast(BitbucketPushContinuationStore, self._continuation)

        async def __aenter__(self) -> PushUow:
            return self

    async def scenario() -> None:
        calls: list[str] = []
        continuation = Continuation()
        use_case = ProjectBitbucketPullRequest(
            authority=Authority(calls),
            current=RateLimitedCurrent(calls),
            uow_factory=lambda: PushUow(calls, continuation),
            push_candidates=PageCandidates(),
            enqueuer=Enqueuer(calls),
        )
        with pytest.raises(BitbucketProviderError) as error:
            await use_case.push(
                TARGET.installation_id,
                TARGET.repository_id,
                TARGET.repository_uuid,
                frozenset({"feature"}),
                6,
                7,
                "receipt-2",
            )
        assert error.value is provider_error
        assert continuation.advances == 0
        assert "enqueue" in calls

    asyncio.run(scenario())
