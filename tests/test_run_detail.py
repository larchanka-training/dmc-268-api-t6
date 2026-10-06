"""Run detail, rerun, T6 signal, SSE coalescing and large responses (#34, api#20 D1/D3/D12)."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from decimal import Decimal
from typing import Self
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.modules.reviews.application.cancel_run import CancelRequestResult, CancelRun
from app.modules.reviews.application.get_run import FindingView, GetRunDetail, RunReview
from app.modules.reviews.application.get_run_comments import PublishedComment
from app.modules.reviews.application.list_runs import RunListItem
from app.modules.reviews.application.queue_messages import StoredRunMessage
from app.modules.reviews.application.rerun_run import (
    RerunConflict,
    RerunNotConfigured,
    RerunOutcome,
    RerunResult,
    RerunRun,
)
from app.modules.reviews.application.run_events import InMemoryRunUpdateHub, RunUpdated
from app.modules.reviews.application.try_enqueue_webhook_run import (
    PendingRunMessage,
    RunPublicationKind,
)
from app.modules.reviews.infrastructure.run_action_payloads import ROW_LIMIT_BYTES, truncated

RUN = UUID("11111111-1111-4111-8111-111111111111")
OTHER = UUID("22222222-2222-4222-8222-222222222222")
NOW = datetime(2026, 9, 30, tzinfo=UTC)


def run_item(status: str = "succeeded", *, summary_only: bool = False) -> RunListItem:
    return RunListItem(
        id=RUN,
        status=status,
        engine="fast",
        attempt=1,
        cancel_requested=False,
        started_at=NOW,
        finished_at=NOW,
        error_code=None,
        model="m",
        action_count=3,
        repo="o/r",
        number=1,
        title="PR",
        url="https://github.test/o/r/pull/1",
        head_sha="a" * 40,
        created_at=NOW,
        summary_only=summary_only,
    )


def finding(severity: str, index: int = 0) -> FindingView:
    return FindingView(
        comment=PublishedComment(
            id=UUID(int=index + 1),
            file="app.py",
            old_line=None,
            new_line=10 + index,
            end_line=None,
            severity=severity,
            category="correctness",
            title=severity,
            body="body",
            rule_name=None,
            created_at=NOW,
        ),
        side="RIGHT",
        suggestion=None,
        confidence=0.9,
    )


def review(*severities: str, usage_calls: int = 1) -> RunReview:
    return RunReview(
        author="octocat",
        head_ref="feature",
        base_ref="main",
        findings=[finding(item, index) for index, item in enumerate(severities)],
        summary={"problem": "p", "done_well": "d", "effort": "small"},
        usage_calls=usage_calls,
        tokens_in=100,
        tokens_out=20,
        cost_usd=Decimal("0.01"),
    )


@dataclass
class Detail:
    item: RunListItem | None
    stored: RunReview | None

    async def get_run(self, run_id: UUID) -> RunListItem | None:
        return self.item

    async def get_run_review(self, run_id: UUID) -> RunReview | None:
        return self.stored


@pytest.mark.parametrize(
    ("severities", "expected"),
    [
        (("low", "critical"), "blocking"),
        (("info", "high"), "blocking"),
        (("medium", "info"), "attention"),
        (("low",), "attention"),
        (("info",), "clean"),
        ((), "clean"),
    ],
)
def test_verdict_follows_the_published_findings(severities: tuple[str, ...], expected: str) -> None:
    detail = asyncio.run(GetRunDetail(Detail(run_item(), review(*severities))).execute(RUN))

    assert detail is not None
    assert detail.verdict == expected
    assert sum(detail.severity_counts.values()) == len(severities)
    for severity in ("critical", "high", "medium", "low", "info"):
        assert detail.severity_counts[severity] == severities.count(severity)


def test_findings_are_ordered_by_severity_and_budget_uses_engine_caps() -> None:
    detail = asyncio.run(
        GetRunDetail(Detail(run_item(), review("info", "critical", "medium"))).execute(RUN)
    )

    assert detail is not None
    assert [item.comment.severity for item in detail.review.findings] == [
        "critical",
        "medium",
        "info",
    ]
    assert detail.budget is not None
    assert (detail.budget.token_limit, detail.budget.cost_limit_usd) == (60_000, Decimal("0.50"))


@pytest.mark.parametrize(
    "item", [run_item("running"), run_item("failed"), run_item(summary_only=True)]
)
def test_verdict_is_null_until_success_and_for_summary_only_runs(item: RunListItem) -> None:
    detail = asyncio.run(GetRunDetail(Detail(item, review("high"))).execute(RUN))
    assert detail is not None and detail.verdict is None


def test_budget_is_null_before_the_first_llm_call() -> None:
    detail = asyncio.run(
        GetRunDetail(Detail(run_item("queued"), review(usage_calls=0))).execute(RUN)
    )
    assert detail is not None and detail.budget is None


def test_run_detail_endpoint_emits_pull_request_refs_and_camel_case_results() -> None:
    from app.main import get_run_repository
    from tests.portal_test_client import authenticated_test_client

    app.dependency_overrides[get_run_repository] = lambda: Detail(run_item(), review("high"))
    try:
        body = authenticated_test_client(app).get(f"/api/runs/{RUN}").json()
    finally:
        app.dependency_overrides.clear()

    assert body["pullRequest"]["author"] == "octocat"
    assert (body["pullRequest"]["headRef"], body["pullRequest"]["baseRef"]) == ("feature", "main")
    assert body["summary"] == {"problem": "p", "doneWell": "d", "effort": "small"}
    assert body["verdict"] == "blocking"
    assert body["severityCounts"]["high"] == 1
    assert body["findings"][0]["newLine"] == 10 and body["findings"][0]["confidence"] == 0.9
    assert body["budget"] == {
        "tokensIn": 100,
        "tokensOut": 20,
        "costUsd": 0.01,
        "tokenLimit": 60000,
        "costLimitUsd": 0.5,
    }


def pending(run_id: UUID) -> StoredRunMessage:
    message = PendingRunMessage(
        run_id=run_id,
        workspace_id=UUID(int=1),
        installation_id=17,
        repository_id=UUID(int=2),
        repository_external_id=101,
        repository_full_name="o/r",
        pr_number=1,
        head_sha="a" * 40,
        base_sha="b" * 40,
        base_ref="main",
        engine="fast",
        rule_version_id=UUID(int=3),
        prompt_version_id=UUID(int=4),
        attempt=1,
        requested_at=NOW,
    )
    return StoredRunMessage(**{**vars(message), "trigger": "rerun"})


@dataclass
class Reruns:
    """Rerun store, its unit of work and the run reader in one fake."""

    result: RerunResult
    published: list[UUID] = field(default_factory=list)
    commits: int = 0

    @property
    def runs(self) -> Reruns:
        return self

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    async def commit(self) -> None:
        self.commits += 1

    async def rollback(self) -> None:
        return None

    async def create_rerun(self, run_id: UUID, now: datetime) -> RerunResult:
        return self.result

    async def mark_rerun_published(self, run_id: UUID, now: datetime) -> None:
        self.published.append(run_id)

    async def get_run(self, run_id: UUID) -> RunListItem | None:
        return replace(run_item("queued"), id=run_id)


@dataclass
class Publisher:
    fail: bool = False
    sent: list[tuple[UUID, str]] = field(default_factory=list)

    async def publish_confirmed(
        self, message: PendingRunMessage, *, kind: RunPublicationKind = RunPublicationKind.QUEUED
    ) -> None:
        if self.fail:
            raise ConnectionError("broker down")
        assert isinstance(message, StoredRunMessage)
        self.sent.append((message.run_id, message.trigger))


def test_rerun_publishes_the_new_run_and_marks_it_published() -> None:
    repository = Reruns(RerunResult(RerunOutcome.CREATED, pending(OTHER)))
    publisher = Publisher()

    item = asyncio.run(
        RerunRun(lambda: repository, repository, publisher, lambda: NOW).execute(RUN)
    )

    assert item is not None and (item.id, item.status) == (OTHER, "queued")
    assert publisher.sent == [(OTHER, "rerun")]
    assert repository.published == [OTHER]
    # The insert and the published mark commit in two short transactions.
    assert repository.commits == 2


def test_rerun_publication_failure_keeps_the_run_queued_for_the_reconciler() -> None:
    repository = Reruns(RerunResult(RerunOutcome.CREATED, pending(OTHER)))

    item = asyncio.run(
        RerunRun(lambda: repository, repository, Publisher(fail=True), lambda: NOW).execute(RUN)
    )

    assert item is not None and item.status == "queued"
    assert repository.published == []


def rerun(result: RerunResult) -> RerunRun:
    repository = Reruns(result)
    return RerunRun(lambda: repository, repository, Publisher())


def test_rerun_conflict_missing_run_and_missing_configuration() -> None:
    with pytest.raises(RerunConflict):
        asyncio.run(rerun(RerunResult(RerunOutcome.CONFLICT)).execute(RUN))
    with pytest.raises(RerunNotConfigured):
        asyncio.run(rerun(RerunResult(RerunOutcome.NOT_CONFIGURED)).execute(RUN))
    assert asyncio.run(rerun(RerunResult(RerunOutcome.NOT_FOUND)).execute(RUN)) is None


@dataclass
class CancelRepository:
    signal: bool
    item: RunListItem = field(default_factory=lambda: run_item("cancelled"))

    @property
    def repository(self) -> CancelRepository:
        return self

    async def __aenter__(self) -> CancelRepository:
        return self

    async def __aexit__(self, *args: object) -> None:
        pass

    async def commit(self) -> None:
        pass

    async def rollback(self) -> None:
        pass

    async def request_cancel(self, run_id: UUID) -> CancelRequestResult:
        return CancelRequestResult(found=True, changed=True, signal_requested=self.signal)

    async def get_run(self, run_id: UUID) -> RunListItem | None:
        return self.item


@dataclass
class Signals:
    calls: list[tuple[UUID, ...]] = field(default_factory=list)

    async def publish_for(self, run_ids: tuple[UUID, ...]) -> int:
        self.calls.append(run_ids)
        return len(run_ids)


@pytest.mark.parametrize("signal", [True, False])
def test_cancel_publishes_the_t6_close_signal_only_for_an_attempted_run(signal: bool) -> None:
    signals = Signals()
    asyncio.run(
        CancelRun(signals=signals, uow_factory=lambda: CancelRepository(signal)).execute(RUN)
    )
    assert signals.calls == ([(RUN,)] if signal else [])


def test_slow_subscriber_gets_the_latest_status_per_run_without_losing_other_runs() -> None:
    hub = InMemoryRunUpdateHub()

    async def scenario() -> list[RunUpdated]:
        async with hub.subscribe() as events:
            for status in ("queued", "running", "publishing"):
                await hub.publish(RunUpdated(RUN, status))
            await hub.publish(RunUpdated(OTHER, "queued"))
            await hub.publish(RunUpdated(RUN, "succeeded"))
            return [await anext(events), await anext(events)]

    assert asyncio.run(scenario()) == [RunUpdated(OTHER, "queued"), RunUpdated(RUN, "succeeded")]


def test_truncated_response_keeps_the_longest_prefix_within_one_mebibyte() -> None:
    raw = json.dumps({"text": "й" * (ROW_LIMIT_BYTES // 2 + 1000)})
    wrapper = truncated(raw)
    size = len(json.dumps(wrapper, ensure_ascii=False, separators=(",", ":")).encode())

    assert wrapper["truncated"] is True
    assert wrapper["original_bytes"] == len(raw.encode())
    assert size <= ROW_LIMIT_BYTES
    assert raw.startswith(wrapper["text"])
    longer = {**wrapper, "text": raw[: len(wrapper["text"]) + 1]}
    assert len(json.dumps(longer, ensure_ascii=False, separators=(",", ":")).encode()) > (
        ROW_LIMIT_BYTES
    )


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("get", "/api/repos"),
        ("get", f"/api/repos/{RUN}"),
        ("patch", f"/api/repos/{RUN}"),
        ("get", f"/api/repos/{RUN}/pulls"),
        ("post", f"/api/runs/{RUN}/rerun"),
    ],
)
def test_new_endpoints_require_a_bearer_token(method: str, path: str) -> None:
    response = TestClient(app).request(method.upper(), path, json={"enabled": True})
    assert response.status_code == 401
