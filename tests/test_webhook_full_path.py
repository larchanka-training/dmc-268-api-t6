"""One signed delivery reaches a confirmed Run and filtered review input."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from types import TracebackType
from typing import Self
from uuid import UUID

from fastapi.testclient import TestClient

from app.bootstrap.reviews_api import get_github_webhook_receipt_uow_factory
from app.main import app, get_github_webhook_secret
from app.modules.integrations.webhooks.api.pull_request_dtos import parse_pull_request_event
from app.modules.integrations.webhooks.application.github_installation_dispatch import (
    GitHubInstallationDeliveryDispatcher,
    VerifiedGitHubDelivery,
)
from app.modules.integrations.webhooks.application.receive_github_delivery import (
    ReceiveGitHubDelivery,
)
from app.modules.reviews.application.determine_ci_eligibility import (
    CheckSuite,
    CiSnapshot,
    CiWaitMode,
    DetermineCiEligibility,
    EligibilityCandidate,
)
from app.modules.reviews.application.get_run_diff import DiffSnapshot, review_files_from_snapshots
from app.modules.reviews.application.process_run import ReviewRunProcessor, RunVcsInput
from app.modules.reviews.application.project_github_pull_request import (
    ProjectGitHubPullRequest,
    PullRequestEvent,
    PullRequestRecord,
)
from app.modules.reviews.application.prompt_builder import DiffLine, PullRequestMeta
from app.modules.reviews.application.trigger_from_delivery import (
    CiTriggerEvent,
    TriggerFromDelivery,
)
from app.modules.reviews.application.try_enqueue_webhook_run import (
    PendingRunMessage,
    RunInsertCandidate,
    TryEnqueueWebhookRun,
)
from app.modules.reviews.application.vcs_diff import PullRequestLocator, VcsFile, VcsPullRequest

_SECRET = "full-path-secret"
_NOW = datetime(2026, 9, 28, 12, 0, tzinfo=UTC)
_HEAD = "a" * 40
_BASE = "b" * 40
_PR_ID = UUID("11111111-1111-1111-1111-111111111111")
_RUN_ID = UUID("22222222-2222-2222-2222-222222222222")
_REPO_ID = UUID("33333333-3333-3333-3333-333333333333")
_WORKSPACE_ID = UUID("44444444-4444-4444-4444-444444444444")
_RULE_ID = UUID("55555555-5555-5555-5555-555555555555")
_PROMPT_ID = UUID("66666666-6666-6666-6666-666666666666")


@dataclass
class Receipt:
    delivery: VerifiedGitHubDelivery
    claim_token: UUID | None = None
    projected: bool = False


@dataclass
class State:
    receipts: dict[str, Receipt] = field(default_factory=dict)
    pull_request: PullRequestRecord | None = None
    message: PendingRunMessage | None = None
    confirmed: list[PendingRunMessage] = field(default_factory=list)
    snapshots: list[DiffSnapshot] | None = None
    open_transactions: int = 0
    commits: int = 0
    notification: tuple[UUID, UUID, str] | None = None
    published: bool = False

    def candidate(self) -> EligibilityCandidate | None:
        pr = self.pull_request
        if pr is None:
            return None
        return EligibilityCandidate(
            code_change_id=pr.id,
            installation_external_id=17,
            repository_full_name="octo/repo",
            head_sha=pr.head_sha,
            state=pr.state,
            repository_enabled=True,
            reviewer_requested=pr.reviewer_requested,
            reviewer_requested_at=pr.reviewer_requested_at,
            head_first_seen_at=pr.head_first_seen_at,
            wait_for_ci=CiWaitMode.ALWAYS,
        )


class ReceiptStore:
    def __init__(self, state: State) -> None:
        self.state = state

    async def save(self, delivery: VerifiedGitHubDelivery) -> bool:
        if delivery.delivery_id in self.state.receipts:
            return False
        self.state.receipts[delivery.delivery_id] = Receipt(delivery)
        return True

    async def pending_ids(self, now: datetime, limit: int) -> tuple[str, ...]:
        return tuple(key for key, row in self.state.receipts.items() if not row.projected)[:limit]

    async def claim(
        self, delivery_id: str, token: UUID, now: datetime, until: datetime
    ) -> VerifiedGitHubDelivery | None:
        row = self.state.receipts[delivery_id]
        if row.projected or row.claim_token is not None:
            return None
        row.claim_token = token
        return row.delivery

    async def mark_projected(self, delivery_id: str, token: UUID, at: datetime) -> None:
        row = self.state.receipts[delivery_id]
        assert row.claim_token == token
        row.projected = True
        row.claim_token = None

    async def release(self, delivery_id: str, token: UUID, retry_after: datetime) -> None:
        raise AssertionError("successful full path must not release its receipt")


class ProjectionStore:
    def __init__(self, state: State) -> None:
        self.state = state

    async def get_or_create_locked(
        self, event: PullRequestEvent, now: datetime
    ) -> PullRequestRecord:
        if self.state.pull_request is None:
            self.state.pull_request = PullRequestRecord.from_event(_PR_ID, _REPO_ID, event, now)
        return self.state.pull_request

    async def save(self, record: PullRequestRecord) -> None:
        self.state.pull_request = record


class RunStore:
    def __init__(self, state: State) -> None:
        self.state = state

    async def cancel_for_pr(self, code_change_id: UUID, reason: str, now: datetime) -> tuple[()]:
        return ()

    async def notify_run_updated(self, *args: object) -> None:
        if len(args) == 3:
            run_id, workspace_id, status = args
            assert isinstance(run_id, UUID) and isinstance(workspace_id, UUID)
            assert isinstance(status, str)
            self.state.notification = (run_id, workspace_id, status)

    async def lock_candidate(self, code_change_id: UUID) -> RunInsertCandidate | None:
        ci = self.state.candidate()
        if ci is None or ci.code_change_id != code_change_id:
            return None
        return RunInsertCandidate(
            ci=ci,
            repository_id=_REPO_ID,
            workspace_id=_WORKSPACE_ID,
            repository_external_id=101,
            pr_number=7,
            base_sha=_BASE,
            base_ref="main",
            engine="fast",
            rule_version_id=_RULE_ID,
            prompt_version_id=_PROMPT_ID,
        )

    async def insert_webhook_run(
        self, candidate: RunInsertCandidate, now: datetime
    ) -> PendingRunMessage | None:
        if self.state.message is not None:
            return None
        self.state.message = PendingRunMessage.from_candidate(_RUN_ID, candidate, now)
        return self.state.message

    async def mark_published(self, run_id: UUID, now: datetime) -> None:
        assert self.state.message is not None and run_id == self.state.message.run_id
        self.state.published = True

    async def pending_messages(self, limit: int) -> tuple[PendingRunMessage, ...]:
        return ()


class MemoryUnitOfWork:
    def __init__(self, state: State) -> None:
        self.state = state

    @property
    def receipts(self) -> ReceiptStore:
        return ReceiptStore(self.state)

    @property
    def pull_requests(self) -> ProjectionStore:
        return ProjectionStore(self.state)

    @property
    def runs(self) -> RunStore:
        return RunStore(self.state)

    async def __aenter__(self) -> Self:
        self.state.open_transactions += 1
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.state.open_transactions -= 1

    async def commit(self) -> None:
        self.state.commits += 1

    async def rollback(self) -> None:
        pass


class Candidates:
    def __init__(self, state: State) -> None:
        self.state = state

    async def get(self, code_change_id: UUID) -> EligibilityCandidate | None:
        candidate = self.state.candidate()
        assert candidate is None or candidate.code_change_id == code_change_id
        return candidate


class GreenCi:
    def __init__(self, state: State) -> None:
        self.state = state

    async def get_current_head_ci(
        self, installation_external_id: int, repository_full_name: str, head_sha: str
    ) -> CiSnapshot:
        assert self.state.open_transactions == 0
        assert (installation_external_id, repository_full_name, head_sha) == (
            17,
            "octo/repo",
            _HEAD,
        )
        return CiSnapshot(_HEAD, (CheckSuite(7, "completed", "success"),), "pending", 0)


class Targets:
    def __init__(self, state: State) -> None:
        self.state = state

    async def for_pr(self, event: PullRequestEvent) -> UUID | None:
        record = self.state.pull_request
        return record.id if record is not None and record.head_sha == event.head_sha else None

    async def for_ci(self, event: CiTriggerEvent) -> tuple[UUID, ...]:
        return ()


class ConfirmedPublisher:
    def __init__(self, state: State) -> None:
        self.state = state

    async def publish_confirmed(self, message: PendingRunMessage) -> None:
        assert self.state.open_transactions == 0
        assert self.state.message == message
        assert self.state.notification == (_RUN_ID, _WORKSPACE_ID, "queued")
        self.state.confirmed.append(message)


class UnusedResolver:
    async def find_github_installation_id(self, external_id: int) -> UUID | None:
        raise AssertionError("PR delivery must not resolve installation onboarding")


class UnusedOnboarding:
    async def execute(self, *, provider_installation_id: UUID, event: object) -> tuple[()]:
        raise AssertionError("PR delivery must not onboard repositories")


class SnapshotRepository:
    def __init__(self, state: State) -> None:
        self.state = state

    async def get_run_diff_input(self, run_id: UUID) -> None:
        raise AssertionError("webhook Run must use the VCS input")

    async def get_run_vcs_input(self, run_id: UUID) -> RunVcsInput:
        assert self.state.published and run_id == _RUN_ID
        return RunVcsInput(_PR_ID, _REPO_ID, _HEAD, _BASE, PullRequestLocator(17, "octo/repo", 7))

    async def get_run_snapshots(self, run_id: UUID) -> list[DiffSnapshot] | None:
        return self.state.snapshots

    async def store_diff_snapshots(
        self, run_id: UUID, code_change_id: UUID, head_sha: str, snapshots: list[DiffSnapshot]
    ) -> list[DiffSnapshot]:
        assert (run_id, code_change_id, head_sha) == (_RUN_ID, _PR_ID, _HEAD)
        self.state.snapshots = snapshots
        return snapshots


class Vcs:
    def __init__(self, state: State) -> None:
        self.state = state

    async def get_pull_request(self, locator: PullRequestLocator) -> VcsPullRequest:
        assert self.state.open_transactions == 0
        assert locator == PullRequestLocator(17, "octo/repo", 7)
        return VcsPullRequest(
            locator,
            _HEAD,
            _BASE,
            PullRequestMeta(
                "Review parser", None, "alice", "feature", "main", (), 2, 1, 1, False, False
            ),
        )

    async def get_diff(self, pull_request: VcsPullRequest) -> tuple[VcsFile, ...]:
        assert self.state.open_transactions == 0
        return (
            VcsFile(
                "src/parser.py", "modified", "1" * 40, None, 1, 1, 2, "@@ -1 +1 @@\n-old\n+new"
            ),
            VcsFile(
                "package-lock.json",
                "modified",
                "2" * 40,
                None,
                1,
                0,
                1,
                "@@ -0,0 +1 @@\n+generated",
            ),
        )

    async def get_blob(self, locator: PullRequestLocator, sha: str) -> bytes:
        raise AssertionError("both files have patches; blob download is unnecessary")


def test_signed_webhook_reaches_confirmed_run_and_filtered_snapshot() -> None:
    state = State()

    def uow_factory() -> MemoryUnitOfWork:
        return MemoryUnitOfWork(state)

    enqueuer = TryEnqueueWebhookRun(
        eligibility=DetermineCiEligibility(
            candidates=Candidates(state), ci=GreenCi(state), own_app_id=42
        ),
        uow_factory=uow_factory,
        publisher=ConfirmedPublisher(state),
        now=lambda: _NOW,
    )
    dispatcher = GitHubInstallationDeliveryDispatcher(
        resolver=UnusedResolver(),
        onboarding=UnusedOnboarding(),
        pull_request_projector=ProjectGitHubPullRequest(
            uow_factory=uow_factory, bot_login="reviewer[bot]", now=lambda: _NOW
        ),
        pull_request_parser=parse_pull_request_event,
        run_trigger=TriggerFromDelivery(targets=Targets(state), enqueuer=enqueuer),
    )
    receiver = ReceiveGitHubDelivery(
        uow_factory=uow_factory, dispatcher=dispatcher, now=lambda: _NOW
    )
    payload = {
        "action": "review_requested",
        "installation": {"id": 17},
        "repository": {"id": 101, "full_name": "octo/repo"},
        "requested_reviewer": {"login": "reviewer[bot]"},
        "sender": {"type": "User"},
        "pull_request": {
            "id": 901,
            "number": 7,
            "title": "Review parser",
            "body": None,
            "html_url": "https://github.com/octo/repo/pull/7",
            "user": {"login": "alice"},
            "head": {"ref": "feature", "sha": _HEAD},
            "base": {"ref": "main", "sha": _BASE},
            "state": "open",
            "updated_at": "2026-09-28T11:59:00Z",
        },
    }
    body = json.dumps(payload).encode()
    signature = hmac.new(_SECRET.encode(), body, hashlib.sha256).hexdigest()
    headers = {
        "X-GitHub-Event": "pull_request",
        "X-GitHub-Delivery": "delivery-full-path",
        "X-Hub-Signature-256": f"sha256={signature}",
    }
    app.dependency_overrides[get_github_webhook_secret] = lambda: _SECRET
    app.dependency_overrides[get_github_webhook_receipt_uow_factory] = lambda: uow_factory
    try:
        client = TestClient(app)
        response = client.post("/webhooks/github", content=body, headers=headers)
        duplicate = client.post("/webhooks/github", content=body, headers=headers)
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 202
    assert response.json() == {"status": "pending"}
    assert duplicate.status_code == 202
    assert duplicate.json() == {"status": "duplicate"}
    assert len(state.receipts) == 1
    assert state.receipts["delivery-full-path"].projected is False
    assert state.pull_request is None and state.message is None

    assert asyncio.run(receiver.replay_pending()) == 1
    assert state.receipts["delivery-full-path"].projected is True
    assert state.pull_request is not None and state.pull_request.reviewer_requested
    assert state.published
    assert len(state.confirmed) == 1
    message = state.confirmed[0]
    assert message.as_payload()["schema"] == "review.run/v1"
    assert (message.run_id, message.head_sha, message.attempt) == (_RUN_ID, _HEAD, 1)

    assert asyncio.run(
        ReviewRunProcessor(SnapshotRepository(state), None, vcs_provider=Vcs(state)).execute(
            _RUN_ID
        )
    )
    assert state.snapshots is not None
    assert [item.filename for item in state.snapshots] == ["src/parser.py", "package-lock.json"]
    changed, omitted = review_files_from_snapshots(state.snapshots)
    assert tuple(file.path for file in changed) == ("src/parser.py",)
    assert changed[0].lines == (DiffLine(1, "removed", "old"), DiffLine(1, "added", "new"))
    assert omitted == ("package-lock.json",)
