"""Canonical Bitbucket PR state drives projection before idempotent run admission."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import UUID

from app.modules.integrations.bitbucket.application.request_credentials import (
    BoundBitbucketTarget,
)
from app.modules.reviews.application.determine_ci_eligibility import WebhookRunIdentity
from app.modules.reviews.application.project_bitbucket_pull_request import (
    BitbucketPullRequestCommand,
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
        return ProjectedBitbucketPullRequest(
            UUID(int=9), 17, current.head_sha, current.state, self.changed
        )


@dataclass
class PushCandidates:
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
class NoCandidates:
    calls: list[str]

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


@dataclass
class Uow:
    calls: list[str]
    pull_requests: Store

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
