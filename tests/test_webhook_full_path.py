"""Signed webhook receipt storage and replay through application dispatch."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from types import TracebackType
from typing import Any, Self
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from app.bootstrap.reviews_api import get_github_webhook_receipt_uow_factory
from app.main import app, get_github_webhook_secret
from app.modules.integrations.webhooks.api.dispatch import GitHubWebhookDispatchAdapter
from app.modules.integrations.webhooks.application.github_installation_dispatch import (
    GitHubInstallationDeliveryDispatcher,
)
from app.modules.integrations.webhooks.application.receive_github_delivery import (
    ReceiveGitHubDelivery,
    WebhookReceipt,
)
from app.modules.reviews.application.determine_ci_eligibility import (
    CheckSuite,
    CiSnapshot,
    CiWaitMode,
    DetermineCiEligibility,
    EligibilityCandidate,
)
from app.modules.reviews.application.get_run_diff import DiffSnapshot
from app.modules.reviews.application.process_run import RunVcsInput
from app.modules.reviews.application.project_github_pull_request import (
    LockedPullRequest,
    ProjectGitHubPullRequest,
    PullRequestEvent,
    PullRequestRecord,
    PullRequestState,
)
from app.modules.reviews.application.prompt_builder import PullRequestMeta
from app.modules.reviews.application.trigger_from_delivery import (
    CiTriggerEvent,
    ProjectedPullRequestTarget,
    TriggerFromDelivery,
)
from app.modules.reviews.application.try_enqueue_webhook_run import (
    CandidateMiss,
    DuplicateReason,
    EnqueueStatus,
    PendingRunMessage,
    RunInsertCandidate,
    RunPublicationKind,
    TryEnqueueWebhookRun,
)
from app.modules.reviews.application.vcs_diff import PullRequestLocator, VcsFile, VcsPullRequest
from tests.trigger_uow import targets_uow

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
    delivery: WebhookReceipt
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
    miss: CandidateMiss | None = None

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
            ai_review_labeled=pr.ai_review_labeled,
            ai_review_labeled_at=pr.ai_review_labeled_at,
            head_first_seen_at=pr.head_first_seen_at,
            wait_for_ci=CiWaitMode.ALWAYS,
        )


class ReceiptStore:
    def __init__(self, state: State) -> None:
        self.state = state

    async def save(self, delivery: WebhookReceipt) -> bool:
        if delivery.delivery_id in self.state.receipts:
            return False
        self.state.receipts[delivery.delivery_id] = Receipt(delivery)
        return True

    async def pending_ids(self, now: datetime, limit: int) -> tuple[str, ...]:
        return tuple(key for key, row in self.state.receipts.items() if not row.projected)[:limit]

    async def claim(
        self, delivery_id: str, token: UUID, now: datetime, until: datetime
    ) -> WebhookReceipt | None:
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

    async def retry_run_trigger(self, delivery_id: str, token: UUID, retry_after: datetime) -> None:
        raise AssertionError("successful full path must not wait for a Run")

    async def release(
        self,
        delivery_id: str,
        token: UUID,
        retry_after: datetime,
        deferred_at: datetime,
        max_attempts: int,
    ) -> bool:
        raise AssertionError("successful full path must not release its receipt")

    async def purge_finished(self, before: datetime) -> int:
        return 0

    async def release_after_dispatch_failure(
        self,
        delivery_id: str,
        token: UUID,
        retry_after: datetime,
        failed_at: datetime,
        max_attempts: int,
    ) -> bool:
        raise AssertionError("successful full path must not release its receipt")


class ProjectionStore:
    def __init__(self, state: State) -> None:
        self.state = state

    async def get_or_create_locked(
        self, event: PullRequestEvent, now: datetime
    ) -> LockedPullRequest:
        created = self.state.pull_request is None
        if self.state.pull_request is None:
            self.state.pull_request = PullRequestRecord.from_event(_PR_ID, _REPO_ID, event, now)
        return LockedPullRequest(self.state.pull_request, created)

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

    async def lock_candidate(self, code_change_id: UUID) -> RunInsertCandidate | CandidateMiss:
        ci = self.state.candidate()
        if ci is None or ci.code_change_id != code_change_id:
            return CandidateMiss.PULL_REQUEST_GONE
        if self.state.miss is not None:
            return self.state.miss
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
    ) -> PendingRunMessage | DuplicateReason:
        if self.state.message is not None:
            return DuplicateReason.ACTIVE_RUN
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

    async def for_pr(self, event: PullRequestEvent) -> ProjectedPullRequestTarget | None:
        record = self.state.pull_request
        return (
            ProjectedPullRequestTarget(record.id, record.head_sha)
            if record is not None and record.state == PullRequestState.OPEN
            else None
        )

    async def for_ci(self, event: CiTriggerEvent) -> tuple[UUID, ...]:
        record = self.state.pull_request
        return (
            (record.id,)
            if record is not None
            and record.state == PullRequestState.OPEN
            and record.head_sha == event.head_sha
            else ()
        )


class ConfirmedPublisher:
    def __init__(self, state: State) -> None:
        self.state = state

    async def publish_confirmed(
        self, message: PendingRunMessage, *, kind: RunPublicationKind = RunPublicationKind.QUEUED
    ) -> None:
        assert kind is RunPublicationKind.QUEUED
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


def test_signed_former_reviewer_webhook_cannot_create_intent_or_run() -> None:
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
        run_trigger=TriggerFromDelivery(uow_factory=targets_uow(Targets(state)), enqueuer=enqueuer),
    )
    receiver = ReceiveGitHubDelivery(
        uow_factory=uow_factory,
        dispatcher=GitHubWebhookDispatchAdapter(dispatcher),
        now=lambda: _NOW,
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
    assert json.loads(state.receipts["delivery-full-path"].delivery.payload_json) == payload
    assert state.pull_request is None and state.message is None

    assert asyncio.run(receiver.replay_pending()) == 1
    assert state.receipts["delivery-full-path"].projected is True
    assert state.pull_request is None
    assert state.message is None
    assert state.confirmed == []
    assert not state.published


def test_signed_ai_review_label_enqueues_after_current_head_green_ci() -> None:
    state = State()
    labels = {"ai-review"}

    def uow_factory() -> MemoryUnitOfWork:
        return MemoryUnitOfWork(state)

    class Current:
        async def get_current(self, event: PullRequestEvent) -> PullRequestEvent:
            assert state.open_transactions == 0
            return replace(event, current_label_names=frozenset(labels))

    class Lock:
        @asynccontextmanager
        async def hold(self, event: PullRequestEvent) -> AsyncIterator[None]:
            yield

    enqueuer = TryEnqueueWebhookRun(
        eligibility=DetermineCiEligibility(
            candidates=Candidates(state), ci=GreenCi(state), own_app_id=42
        ),
        uow_factory=uow_factory,
        publisher=ConfirmedPublisher(state),
        now=lambda: _NOW,
    )
    projector = ProjectGitHubPullRequest(
        uow_factory=uow_factory,
        bot_login="reviewer[bot]",
        current_provider=Current(),
        projection_lock=Lock(),
        now=lambda: _NOW,
    )
    dispatcher = GitHubInstallationDeliveryDispatcher(
        resolver=UnusedResolver(),
        onboarding=UnusedOnboarding(),
        label_intent_projector=projector,
        run_trigger=TriggerFromDelivery(uow_factory=targets_uow(Targets(state)), enqueuer=enqueuer),
    )
    receiver = ReceiveGitHubDelivery(
        uow_factory=uow_factory,
        dispatcher=GitHubWebhookDispatchAdapter(dispatcher),
        now=lambda: _NOW,
    )
    payload = {
        "action": "labeled",
        "installation": {"id": 17},
        "repository": {"id": 101, "full_name": "octo/repo"},
        "label": {"name": "ai-review"},
        "pull_request": {
            "id": 901,
            "number": 7,
            "title": "Review parser",
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
        "X-GitHub-Delivery": "delivery-ai-review",
        "X-Hub-Signature-256": f"sha256={signature}",
    }
    app.dependency_overrides[get_github_webhook_secret] = lambda: _SECRET
    app.dependency_overrides[get_github_webhook_receipt_uow_factory] = lambda: uow_factory
    try:
        response = TestClient(app).post("/webhooks/github", content=body, headers=headers)
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 202
    assert state.message is None
    assert asyncio.run(receiver.replay_pending()) == 1
    assert state.pull_request is not None and state.pull_request.ai_review_labeled
    assert state.message is not None and state.message.head_sha == _HEAD
    assert state.published and len(state.confirmed) == 1
    assert state.notification == (_RUN_ID, _WORKSPACE_ID, "queued")
    assert state.receipts["delivery-ai-review"].projected

    labels.clear()
    payload["action"] = "unlabeled"
    asyncio.run(
        receiver.execute(WebhookReceipt("delivery-unlabel", "pull_request", json.dumps(payload)))
    )
    assert asyncio.run(receiver.replay_pending()) == 1
    assert state.pull_request is not None and not state.pull_request.ai_review_labeled
    assert asyncio.run(enqueuer.execute(_PR_ID, _HEAD)).status is EnqueueStatus.INELIGIBLE
    assert len(state.confirmed) == 1


_DELIVERY_LOGGER = "app.modules.integrations.webhooks.application.receive_github_delivery"


class BlockedCi(GreenCi):
    async def get_current_head_ci(
        self, installation_external_id: int, repository_full_name: str, head_sha: str
    ) -> CiSnapshot:
        snapshot = await super().get_current_head_ci(
            installation_external_id, repository_full_name, head_sha
        )
        return replace(snapshot, check_suites=(CheckSuite(7, "in_progress", None),))


class LabelCurrent:
    async def get_current(self, event: PullRequestEvent) -> PullRequestEvent:
        return replace(event, current_label_names=frozenset({"ai-review"}))


class LabelLock:
    @asynccontextmanager
    async def hold(self, event: PullRequestEvent) -> AsyncIterator[None]:
        yield


def _label_payload(
    label: str = "ai-review",
    *,
    sender: dict[str, str] | None = None,
    pr_state: str = "open",
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "action": "labeled",
        "installation": {"id": 17},
        "repository": {"id": 101, "full_name": "octo/repo"},
        "label": {"name": label},
        "pull_request": {
            "id": 901,
            "number": 7,
            "title": "Review parser",
            "html_url": "https://github.com/octo/repo/pull/7",
            "user": {"login": "alice"},
            "head": {"ref": "feature", "sha": _HEAD},
            "base": {"ref": "main", "sha": _BASE},
            "state": pr_state,
            "updated_at": "2026-09-28T11:59:00Z",
        },
    }
    if sender is not None:
        payload["sender"] = sender
    return payload


def _outcome_lines(
    state: State,
    caplog: pytest.LogCaptureFixture,
    payload: dict[str, Any],
    *,
    ci: GreenCi | None = None,
    event_name: str = "pull_request",
    delivery_id: str = "delivery-outcome",
) -> list[str]:
    def uow_factory() -> MemoryUnitOfWork:
        return MemoryUnitOfWork(state)

    enqueuer = TryEnqueueWebhookRun(
        eligibility=DetermineCiEligibility(
            candidates=Candidates(state), ci=ci or GreenCi(state), own_app_id=42
        ),
        uow_factory=uow_factory,
        publisher=ConfirmedPublisher(state),
        now=lambda: _NOW,
    )
    dispatcher = GitHubInstallationDeliveryDispatcher(
        resolver=UnusedResolver(),
        onboarding=UnusedOnboarding(),
        label_intent_projector=ProjectGitHubPullRequest(
            uow_factory=uow_factory,
            bot_login="reviewer[bot]",
            current_provider=LabelCurrent(),
            projection_lock=LabelLock(),
            now=lambda: _NOW,
        ),
        run_trigger=TriggerFromDelivery(uow_factory=targets_uow(Targets(state)), enqueuer=enqueuer),
    )
    receiver = ReceiveGitHubDelivery(
        uow_factory=uow_factory,
        dispatcher=GitHubWebhookDispatchAdapter(dispatcher),
        now=lambda: _NOW,
    )
    asyncio.run(receiver.execute(WebhookReceipt(delivery_id, event_name, json.dumps(payload))))
    with caplog.at_level(logging.INFO, logger=_DELIVERY_LOGGER):
        assert asyncio.run(receiver.replay_pending()) == 1
    return [
        record.getMessage()
        for record in caplog.records
        if record.name == _DELIVERY_LOGGER and delivery_id in record.getMessage()
    ]


_LINE = "GitHub webhook delivery delivery-outcome event=pull_request"
_PR_LINE = f"{_LINE} status=projected_pr detail=action=labeled pr={_PR_ID} head=aaaaaaa:"


def test_label_delivery_that_enqueues_a_run_logs_the_run(
    caplog: pytest.LogCaptureFixture,
) -> None:
    state = State()

    lines = _outcome_lines(state, caplog, _label_payload())

    assert lines == [f"{_PR_LINE} enqueued run={_RUN_ID}"]
    assert state.published


def test_label_delivery_blocked_by_foreign_ci_logs_the_gate_reason(
    caplog: pytest.LogCaptureFixture,
) -> None:
    state = State()

    lines = _outcome_lines(state, caplog, _label_payload(), ci=BlockedCi(state))

    assert lines == [f"{_PR_LINE} ineligible (ci_blocked: check suite app=7 in_progress)"]
    assert state.message is None


def test_label_delivery_without_rules_logs_unconfigured_not_stale(
    caplog: pytest.LogCaptureFixture,
) -> None:
    state = State(miss=CandidateMiss.MISSING_RULES)

    lines = _outcome_lines(state, caplog, _label_payload())

    assert lines == [f"{_PR_LINE} unconfigured (missing_rules)"]
    assert state.message is None


def test_label_delivery_for_a_closed_pr_logs_that_no_open_pr_exists(
    caplog: pytest.LogCaptureFixture,
) -> None:
    state = State()

    lines = _outcome_lines(state, caplog, _label_payload(pr_state="closed"))

    assert lines == [f"{_LINE} status=projected_pr detail=action=labeled no open pull request"]
    assert state.message is None


def test_foreign_label_delivery_logs_that_the_label_is_not_ai_review(
    caplog: pytest.LogCaptureFixture,
) -> None:
    state = State()

    lines = _outcome_lines(state, caplog, _label_payload("bug"))

    assert lines == [
        f"{_LINE} status=ignored_irrelevant_event detail=action=labeled label is not ai-review"
    ]
    assert state.pull_request is None


def test_label_delivery_from_the_apps_own_bot_logs_the_ignored_projection(
    caplog: pytest.LogCaptureFixture,
) -> None:
    state = State()
    sender = {"type": "Bot", "login": "reviewer[bot]"}

    lines = _outcome_lines(state, caplog, _label_payload(sender=sender))

    assert lines == [
        f"{_LINE} status=ignored_irrelevant_event detail=action=labeled ignored_own_bot"
    ]
    assert state.pull_request is None


def test_completed_check_suite_logs_the_ci_outcome_of_each_open_pr_on_its_head(
    caplog: pytest.LogCaptureFixture,
) -> None:
    state = State()
    # The label arrives while CI still runs, so only the completed suite can start the Run.
    assert _outcome_lines(state, caplog, _label_payload(), ci=BlockedCi(state)) == [
        f"{_PR_LINE} ineligible (ci_blocked: check suite app=7 in_progress)"
    ]
    check_suite = {
        "action": "completed",
        "installation": {"id": 17},
        "repository": {"id": 101, "full_name": "octo/repo"},
        "check_suite": {"head_sha": _HEAD},
    }

    lines = _outcome_lines(
        state, caplog, check_suite, event_name="check_suite", delivery_id="delivery-ci"
    )

    # A CI event has no action; it lists one outcome per open PR on the head.
    assert lines == [
        "GitHub webhook delivery delivery-ci event=check_suite status=processed_ci "
        f"detail=pr={_PR_ID} head=aaaaaaa: enqueued run={_RUN_ID}"
    ]
    assert state.published
