"""Verified GitHub webhook receipts are durable before projection."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from types import TracebackType
from typing import cast
from uuid import UUID

import httpx
import psycopg
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy.exc import OperationalError
from sqlalchemy.exc import TimeoutError as PoolTimeoutError

from app.bootstrap.reviews_api import get_github_webhook_receipt_uow_factory
from app.main import (
    app,
    get_github_webhook_secret,
)
from app.modules.integrations.webhooks.api.dispatch import GitHubWebhookDispatchAdapter, action_of
from app.modules.integrations.webhooks.api.receipt import VerifiedGitHubDelivery
from app.modules.integrations.webhooks.application.github_installation_dispatch import (
    GitHubDispatchEvent,
    InstallationDeliveryDispatchResult,
    InstallationDeliveryDispatchStatus,
)
from app.modules.integrations.webhooks.application.receive_github_delivery import (
    FailureCategory,
    ReceiveGitHubDelivery,
    WebhookReceipt,
)
from app.modules.integrations.webhooks.infrastructure.failure_category import classify_failure
from app.modules.reviews.application.project_github_pull_request import (
    PullRequestLabelEvent,
)
from app.modules.reviews.application.trigger_from_delivery import CiTriggerEvent
from tests.github_webhook_fakes import (
    _DELIVERY_LOGGER,
    _SECRET,
    FakeClaimedReceipt,
    FakeDispatcher,
    FakeReceiptUnitOfWork,
    _headers,
)


def _outcome_lines(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [
        record.getMessage()
        for record in caplog.records
        if record.levelno == logging.INFO
        and record.getMessage().startswith("GitHub webhook delivery ")
    ]


def test_projected_delivery_logs_one_outcome_line_with_its_detail(
    caplog: pytest.LogCaptureFixture,
) -> None:
    now = datetime(2026, 9, 28, tzinfo=UTC)
    uow = FakeReceiptUnitOfWork()
    receipts = [
        VerifiedGitHubDelivery("outcome-1", "pull_request", {"action": "labeled"}).to_receipt(),
        VerifiedGitHubDelivery("outcome-2", "installation", {"action": "created"}).to_receipt(),
    ]

    class Dispatcher:
        async def execute(self, delivery: WebhookReceipt) -> InstallationDeliveryDispatchResult:
            if delivery.delivery_id == "outcome-1":
                return InstallationDeliveryDispatchResult(
                    InstallationDeliveryDispatchStatus.PROJECTED_PR,
                    "action=labeled pr=p head=aaaaaaa: ineligible (ci_blocked)",
                )
            return InstallationDeliveryDispatchResult(InstallationDeliveryDispatchStatus.ONBOARDED)

    receiver = ReceiveGitHubDelivery(
        uow_factory=lambda: uow, dispatcher=Dispatcher(), now=lambda: now
    )
    for receipt in receipts:
        asyncio.run(receiver.execute(receipt))

    with caplog.at_level(logging.INFO):
        assert asyncio.run(receiver.replay_pending()) == 2

    assert _outcome_lines(caplog) == [
        "GitHub webhook delivery outcome-1 event=pull_request status=projected_pr "
        "detail=action=labeled pr=p head=aaaaaaa: ineligible (ci_blocked)",
        "GitHub webhook delivery outcome-2 event=installation status=onboarded detail=-",
    ]
    assert [record for record in caplog.records if record.levelno >= logging.WARNING] == []


def test_deferred_delivery_logs_when_it_is_tried_again_and_then_that_it_is_final(
    caplog: pytest.LogCaptureFixture,
) -> None:
    now = datetime(2026, 9, 28, tzinfo=UTC)
    uow = FakeReceiptUnitOfWork()
    delivery = VerifiedGitHubDelivery("deferred-1", "pull_request", {"action": "labeled"})

    class UnknownRepositoryDispatcher:
        async def execute(self, delivery: WebhookReceipt) -> InstallationDeliveryDispatchResult:
            return InstallationDeliveryDispatchResult(
                InstallationDeliveryDispatchStatus.IGNORED_UNKNOWN_REPOSITORY, "unknown_repository"
            )

    receiver = ReceiveGitHubDelivery(
        uow_factory=lambda: uow, dispatcher=UnknownRepositoryDispatcher(), now=lambda: now
    )
    asyncio.run(receiver.execute(delivery.to_receipt()))

    with caplog.at_level(logging.INFO):
        for _ in range(3):
            assert asyncio.run(receiver.replay_pending()) == 1
            now += timedelta(minutes=6)

    prefix = (
        "GitHub webhook delivery deferred-1 event=pull_request "
        "status=ignored_unknown_repository detail=unknown_repository"
    )
    assert _outcome_lines(caplog) == [
        f"{prefix} retry_at=2026-09-28T00:05:00+00:00",
        f"{prefix} retry_at=2026-09-28T00:11:00+00:00",
        f"{prefix} retry_at=none",
    ]
    assert [
        record.getMessage() for record in caplog.records if record.levelno == logging.WARNING
    ] == [
        "GitHub webhook delivery deferred-1 deferred after its last attempt: "
        "ignored_unknown_repository"
    ]


def test_known_event_without_a_handler_logs_that_it_is_tried_again(
    caplog: pytest.LogCaptureFixture,
) -> None:
    now = datetime(2026, 9, 28, tzinfo=UTC)
    uow = FakeReceiptUnitOfWork()
    delivery = VerifiedGitHubDelivery("known-1", "pull_request", {"action": "synchronize"})

    class NoTriggerDispatcher:
        async def execute(self, delivery: WebhookReceipt) -> InstallationDeliveryDispatchResult:
            return InstallationDeliveryDispatchResult(
                InstallationDeliveryDispatchStatus.DEFERRED_KNOWN_EVENT, "action=synchronize"
            )

    receiver = ReceiveGitHubDelivery(
        uow_factory=lambda: uow, dispatcher=NoTriggerDispatcher(), now=lambda: now
    )
    asyncio.run(receiver.execute(delivery.to_receipt()))

    with caplog.at_level(logging.INFO):
        assert asyncio.run(receiver.replay_pending()) == 1

    assert _outcome_lines(caplog) == [
        "GitHub webhook delivery known-1 event=pull_request status=deferred_known_event "
        "detail=action=synchronize retry_at=2026-09-28T00:05:00+00:00"
    ]
    assert uow.rows["known-1"].projected is False


_SECRET_IN_MESSAGE = "ghs_t72_sentinel"
_NOW = datetime(2026, 9, 28, tzinfo=UTC)


def _pull_request_receipt(delivery_id: str, action: str = "labeled") -> WebhookReceipt:
    payload: dict[str, object] = {
        "action": action,
        "installation": {"id": 17},
        "repository": {"id": 101, "full_name": "octo/repo"},
        "pull_request": {
            "id": 901,
            "number": 7,
            "title": "Review parser",
            "html_url": "https://github.com/octo/repo/pull/7",
            "user": {"login": "alice"},
            "head": {"ref": "feature", "sha": "a" * 40},
            "base": {"ref": "main", "sha": "b" * 40},
            "state": "open",
            "updated_at": "2026-09-28T11:59:00Z",
        },
    }
    if action in {"labeled", "unlabeled"}:
        payload["label"] = {"name": "ai-review"}
    return VerifiedGitHubDelivery(delivery_id, "pull_request", payload).to_receipt()


@dataclass
class FailingTypedDispatcher:
    """The typed dispatcher behind the real adapter: it raises ``error`` or hangs."""

    error: BaseException | None = None

    async def execute(self, delivery: GitHubDispatchEvent) -> InstallationDeliveryDispatchResult:
        if self.error is not None:
            raise self.error
        await asyncio.sleep(10)
        raise AssertionError("the dispatch timeout did not cancel the dispatcher")


def _failure_lines(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [
        record.getMessage()
        for record in caplog.records
        if record.levelno == logging.WARNING
        and record.getMessage().startswith("GitHub webhook delivery ")
        and " failed stage=" in record.getMessage()
    ]


def _sweep_failure_records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [record for record in caplog.records if record.levelno == logging.ERROR]


def _fallback_records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [
        record
        for record in caplog.records
        if record.levelno == logging.WARNING and " failure line: " in record.getMessage()
    ]


def _dispatch_failure_run(
    receipt: WebhookReceipt,
    typed: FailingTypedDispatcher,
    caplog: pytest.LogCaptureFixture,
    *,
    dispatch_timeout_seconds: float = 1.0,
    reader: Callable[[WebhookReceipt], str | None] = action_of,
    classifier: Callable[[BaseException], FailureCategory] | None = classify_failure,
) -> FakeReceiptUnitOfWork:
    uow = FakeReceiptUnitOfWork()
    receiver = ReceiveGitHubDelivery(
        uow_factory=lambda: uow,
        dispatcher=GitHubWebhookDispatchAdapter(typed),
        now=lambda: _NOW,
        dispatch_timeout_seconds=dispatch_timeout_seconds,
        action_of=reader,
        classify_failure=classifier,
    )
    asyncio.run(receiver.execute(receipt))
    with caplog.at_level(logging.INFO, logger=_DELIVERY_LOGGER):
        assert asyncio.run(receiver.replay_pending()) == 0
    return uow


def _http_status_error() -> httpx.HTTPStatusError:
    request = httpx.Request("GET", f"https://api.github.test/pulls/7?t={_SECRET_IN_MESSAGE}")
    return httpx.HTTPStatusError(
        f"Server error for {_SECRET_IN_MESSAGE}", request=request, response=httpx.Response(500)
    )


@pytest.mark.parametrize(
    ("error", "category", "error_name"),
    [
        (httpx.ConnectError(f"refused {_SECRET_IN_MESSAGE}"), "github_request", "ConnectError"),
        (_http_status_error(), "github_request", "HTTPStatusError"),
        (httpx.ReadTimeout(f"slow {_SECRET_IN_MESSAGE}"), "github_request", "ReadTimeout"),
        (
            OperationalError("SELECT 1", {}, Exception(f"lost {_SECRET_IN_MESSAGE}")),
            "database",
            "OperationalError",
        ),
        (TimeoutError(f"slow {_SECRET_IN_MESSAGE}"), "timeout", "TimeoutError"),
        (RuntimeError(f"boom {_SECRET_IN_MESSAGE}"), "internal", "RuntimeError"),
    ],
)
def test_failed_label_dispatch_logs_its_action_and_category_without_the_error_message(
    caplog: pytest.LogCaptureFixture, error: Exception, category: str, error_name: str
) -> None:
    uow = _dispatch_failure_run(
        _pull_request_receipt("fail-1"), FailingTypedDispatcher(error), caplog
    )

    lines = _failure_lines(caplog)
    assert lines == [
        "GitHub webhook delivery fail-1 event=pull_request action=labeled "
        f"failed stage=dispatch category={category} error={error_name}"
    ]
    assert _SECRET_IN_MESSAGE not in lines[0]
    # The receipt is released for a retry exactly as before.
    row = uow.rows["fail-1"]
    assert row.projected is False
    assert row.claim_token is None
    assert row.retry_after == _NOW + timedelta(seconds=30)
    # The line comes first, then the traceback record; the sweep still sees the original error.
    assert [
        record.levelno
        for record in caplog.records
        if record.name == _DELIVERY_LOGGER and record.levelno >= logging.WARNING
    ] == [logging.WARNING, logging.ERROR]
    (sweep_record,) = _sweep_failure_records(caplog)
    assert sweep_record.getMessage() == "GitHub webhook projection failed for delivery fail-1"
    assert sweep_record.exc_info is None
    assert vars(sweep_record)["error_type"] == type(error).__name__
    assert _SECRET_IN_MESSAGE not in caplog.text


def test_dispatch_timeout_logs_the_timeout_category_and_the_action(
    caplog: pytest.LogCaptureFixture,
) -> None:
    uow = _dispatch_failure_run(
        _pull_request_receipt("slow-1"),
        FailingTypedDispatcher(),
        caplog,
        dispatch_timeout_seconds=0.001,
    )

    assert _failure_lines(caplog) == [
        "GitHub webhook delivery slow-1 event=pull_request action=labeled "
        "failed stage=dispatch category=timeout error=TimeoutError"
    ]
    assert uow.rows["slow-1"].retry_after == _NOW + timedelta(seconds=30)


@pytest.mark.parametrize(
    ("receipt", "event", "action"),
    [
        (_pull_request_receipt("event-1", "synchronize"), "pull_request", "synchronize"),
        (_pull_request_receipt("event-2", "unlabeled"), "pull_request", "unlabeled"),
        (
            VerifiedGitHubDelivery(
                "event-3",
                "check_suite",
                {
                    "action": "completed",
                    "installation": {"id": 17},
                    "repository": {"id": 101},
                    "check_suite": {"head_sha": "a" * 40},
                },
            ).to_receipt(),
            "check_suite",
            "completed",
        ),
        (
            VerifiedGitHubDelivery(
                "event-4",
                "status",
                {"installation": {"id": 17}, "repository": {"id": 101}, "sha": "a" * 40},
            ).to_receipt(),
            "status",
            "-",
        ),
    ],
)
def test_failed_dispatch_of_any_event_logs_its_own_action(
    caplog: pytest.LogCaptureFixture, receipt: WebhookReceipt, event: str, action: str
) -> None:
    _dispatch_failure_run(receipt, FailingTypedDispatcher(RuntimeError("boom")), caplog)

    assert _failure_lines(caplog) == [
        f"GitHub webhook delivery {receipt.delivery_id} event={event} action={action} "
        "failed stage=dispatch category=internal error=RuntimeError"
    ]
    # A payload without an action is no error, so no fallback record is written.
    assert _fallback_records(caplog) == []


def test_failure_line_survives_a_failing_release_of_the_receipt(
    caplog: pytest.LogCaptureFixture,
) -> None:
    @dataclass
    class ReleaseFailsUnitOfWork(FakeReceiptUnitOfWork):
        async def release_after_dispatch_failure(
            self,
            delivery_id: str,
            token: UUID,
            retry_after: datetime,
            failed_at: datetime,
            max_attempts: int,
        ) -> bool:
            raise OperationalError("UPDATE", {}, Exception("connection lost"))

    uow = ReleaseFailsUnitOfWork()
    receiver = ReceiveGitHubDelivery(
        uow_factory=lambda: uow,
        dispatcher=GitHubWebhookDispatchAdapter(
            FailingTypedDispatcher(httpx.ConnectError("refused"))
        ),
        now=lambda: _NOW,
        action_of=action_of,
        classify_failure=classify_failure,
    )
    asyncio.run(receiver.execute(_pull_request_receipt("release-1")))
    with caplog.at_level(logging.INFO, logger=_DELIVERY_LOGGER):
        assert asyncio.run(receiver.replay_pending()) == 0

    assert _failure_lines(caplog) == [
        "GitHub webhook delivery release-1 event=pull_request action=labeled "
        "failed stage=dispatch category=github_request error=ConnectError"
    ]
    (sweep_record,) = _sweep_failure_records(caplog)
    assert sweep_record.exc_info is None
    assert vars(sweep_record)["error_type"] == OperationalError.__name__


def _warning_and_error_records(caplog: pytest.LogCaptureFixture) -> list[tuple[int, str]]:
    return [
        (record.levelno, record.getMessage())
        for record in caplog.records
        if record.name == _DELIVERY_LOGGER and record.levelno >= logging.WARNING
    ]


def test_only_the_last_failed_attempt_logs_that_the_delivery_failed(
    caplog: pytest.LogCaptureFixture,
) -> None:
    clock = [_NOW]
    uow = FakeReceiptUnitOfWork()
    receiver = ReceiveGitHubDelivery(
        uow_factory=lambda: uow,
        dispatcher=GitHubWebhookDispatchAdapter(
            FailingTypedDispatcher(httpx.ConnectError("refused"))
        ),
        now=lambda: clock[0],
        action_of=action_of,
        classify_failure=classify_failure,
    )
    asyncio.run(receiver.execute(_pull_request_receipt("last-1")))

    per_attempt: list[list[tuple[int, str]]] = []
    for _ in range(3):
        caplog.clear()
        with caplog.at_level(logging.INFO, logger=_DELIVERY_LOGGER):
            assert asyncio.run(receiver.replay_pending()) == 0
        per_attempt.append(_warning_and_error_records(caplog))
        clock[0] += timedelta(seconds=31)

    failure = (
        logging.WARNING,
        "GitHub webhook delivery last-1 event=pull_request action=labeled "
        "failed stage=dispatch category=github_request error=ConnectError",
    )
    traceback_line = (logging.ERROR, "GitHub webhook projection failed for delivery last-1")
    assert per_attempt == [
        [failure, traceback_line],
        [failure, traceback_line],
        [
            failure,
            (
                logging.WARNING,
                "GitHub webhook delivery last-1 failed after its last attempt: ConnectError",
            ),
            traceback_line,
        ],
    ]
    assert uow.rows["last-1"].projection_failed_at == _NOW + timedelta(seconds=62)


def test_last_failed_attempt_is_not_logged_as_final_when_its_release_is_not_committed(
    caplog: pytest.LogCaptureFixture,
) -> None:
    @dataclass
    class FinalReleaseCommitFailsUnitOfWork(FakeReceiptUnitOfWork):
        released: bool = False

        async def release_after_dispatch_failure(
            self,
            delivery_id: str,
            token: UUID,
            retry_after: datetime,
            failed_at: datetime,
            max_attempts: int,
        ) -> bool:
            self.released = True
            return True

        async def commit(self) -> None:
            if self.released:
                raise OperationalError("COMMIT", {}, Exception("connection lost"))
            await super().commit()

    uow = FinalReleaseCommitFailsUnitOfWork()
    receiver = ReceiveGitHubDelivery(
        uow_factory=lambda: uow,
        dispatcher=GitHubWebhookDispatchAdapter(FailingTypedDispatcher(RuntimeError("boom"))),
        now=lambda: _NOW,
        action_of=action_of,
        classify_failure=classify_failure,
    )
    asyncio.run(receiver.execute(_pull_request_receipt("uncommitted-1")))
    with caplog.at_level(logging.INFO, logger=_DELIVERY_LOGGER):
        assert asyncio.run(receiver.replay_pending()) == 0

    assert _warning_and_error_records(caplog) == [
        (
            logging.WARNING,
            "GitHub webhook delivery uncommitted-1 event=pull_request action=labeled "
            "failed stage=dispatch category=internal error=RuntimeError",
        ),
        (logging.ERROR, "GitHub webhook projection failed for delivery uncommitted-1"),
    ]


@pytest.mark.parametrize("failing_step", ["mark_projected", "commit"])
def test_failure_after_a_successful_dispatch_logs_the_outcome_that_may_exist(
    caplog: pytest.LogCaptureFixture, failing_step: str
) -> None:
    @dataclass
    class FinalizeFailsUnitOfWork(FakeReceiptUnitOfWork):
        dispatched: bool = False

        async def mark_projected(self, delivery_id: str, token: UUID, at: datetime) -> None:
            if failing_step == "mark_projected":
                raise OperationalError("UPDATE", {}, Exception("connection lost"))
            await super().mark_projected(delivery_id, token, at)

        async def commit(self) -> None:
            if failing_step == "commit" and self.dispatched:
                raise OperationalError("COMMIT", {}, Exception("connection lost"))
            await super().commit()

    uow = FinalizeFailsUnitOfWork()

    class Dispatcher:
        async def execute(self, delivery: WebhookReceipt) -> InstallationDeliveryDispatchResult:
            uow.dispatched = True
            return InstallationDeliveryDispatchResult(
                InstallationDeliveryDispatchStatus.PROJECTED_PR,
                "action=labeled pr=p head=aaaaaaa: enqueued run=r",
            )

    receiver = ReceiveGitHubDelivery(
        uow_factory=lambda: uow,
        dispatcher=Dispatcher(),
        now=lambda: _NOW,
        action_of=action_of,
        classify_failure=classify_failure,
    )
    asyncio.run(receiver.execute(_pull_request_receipt("final-1")))
    with caplog.at_level(logging.INFO, logger=_DELIVERY_LOGGER):
        assert asyncio.run(receiver.replay_pending()) == 0

    assert _failure_lines(caplog) == [
        "GitHub webhook delivery final-1 event=pull_request action=labeled "
        "failed stage=finalize category=database error=OperationalError "
        "outcome=projected_pr detail=action=labeled pr=p head=aaaaaaa: enqueued run=r"
    ]
    if failing_step == "mark_projected":  # the fake commit does not roll back
        assert uow.rows["final-1"].projected is False
    # The sweep keeps the exception class, without rendering its message/traceback.
    (sweep_record,) = _sweep_failure_records(caplog)
    assert sweep_record.exc_info is None
    assert vars(sweep_record)["error_type"] == OperationalError.__name__


def test_failure_to_claim_a_receipt_logs_the_claim_stage_without_event_or_action(
    caplog: pytest.LogCaptureFixture,
) -> None:
    @dataclass
    class ClaimFailsUnitOfWork(FakeReceiptUnitOfWork):
        async def claim(
            self, delivery_id: str, token: UUID, now: datetime, until: datetime
        ) -> WebhookReceipt | None:
            raise OperationalError("UPDATE", {}, Exception(f"lost {_SECRET_IN_MESSAGE}"))

    uow = ClaimFailsUnitOfWork()
    receiver = ReceiveGitHubDelivery(
        uow_factory=lambda: uow,
        dispatcher=FakeDispatcher(),
        now=lambda: _NOW,
        action_of=action_of,
        classify_failure=classify_failure,
    )
    asyncio.run(receiver.execute(_pull_request_receipt("claim-1")))
    with caplog.at_level(logging.INFO, logger=_DELIVERY_LOGGER):
        assert asyncio.run(receiver.replay_pending()) == 0

    assert _failure_lines(caplog) == [
        "GitHub webhook delivery claim-1 event=- action=- "
        "failed stage=claim category=database error=OperationalError"
    ]
    (sweep_record,) = _sweep_failure_records(caplog)
    assert sweep_record.exc_info is None
    assert vars(sweep_record)["error_type"] == OperationalError.__name__


def test_unexpected_failure_while_parsing_a_delivery_still_logs_its_action(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken_parse(payload: Mapping[str, object]) -> PullRequestLabelEvent:
        raise RuntimeError(f"parser bug {_SECRET_IN_MESSAGE}")

    monkeypatch.setattr(
        "app.modules.integrations.webhooks.api.dispatch.parse_pull_request_label_event",
        broken_parse,
    )

    _dispatch_failure_run(_pull_request_receipt("parse-1"), FailingTypedDispatcher(), caplog)

    lines = _failure_lines(caplog)
    assert lines == [
        "GitHub webhook delivery parse-1 event=pull_request action=labeled "
        "failed stage=dispatch category=internal error=RuntimeError"
    ]
    assert _SECRET_IN_MESSAGE not in lines[0]


def test_a_failing_action_reader_cannot_hide_the_failure_line(
    caplog: pytest.LogCaptureFixture,
) -> None:
    def broken_reader(receipt: WebhookReceipt) -> str | None:
        raise RuntimeError(f"reader bug {_SECRET_IN_MESSAGE}")

    uow = _dispatch_failure_run(
        _pull_request_receipt("reader-1"),
        FailingTypedDispatcher(httpx.ConnectError("refused")),
        caplog,
        reader=broken_reader,
    )

    assert _failure_lines(caplog) == [
        "GitHub webhook delivery reader-1 event=pull_request action=- "
        "failed stage=dispatch category=github_request error=ConnectError"
    ]
    # The fallback is never silent: it names the field and the error class, never the message.
    (fallback,) = _fallback_records(caplog)
    assert fallback.getMessage() == (
        "GitHub webhook delivery reader-1 failure line: action fell back to - after RuntimeError"
    )
    assert fallback.exc_info is None
    assert all(_SECRET_IN_MESSAGE not in record.getMessage() for record in caplog.records)
    assert uow.rows["reader-1"].retry_after == _NOW + timedelta(seconds=30)
    (sweep_record,) = _sweep_failure_records(caplog)
    assert sweep_record.exc_info is None
    assert vars(sweep_record)["error_type"] == httpx.ConnectError.__name__


def test_an_undecodable_payload_is_logged_as_a_reader_fallback_not_as_no_action(
    caplog: pytest.LogCaptureFixture,
) -> None:
    class RaisingDispatcher:
        async def execute(self, delivery: WebhookReceipt) -> InstallationDeliveryDispatchResult:
            raise httpx.ConnectError("refused")

    uow = FakeReceiptUnitOfWork()
    receiver = ReceiveGitHubDelivery(
        uow_factory=lambda: uow,
        dispatcher=RaisingDispatcher(),
        now=lambda: _NOW,
        action_of=action_of,
        classify_failure=classify_failure,
    )
    asyncio.run(receiver.execute(WebhookReceipt("undecodable-1", "pull_request", "not json")))
    with caplog.at_level(logging.INFO, logger=_DELIVERY_LOGGER):
        assert asyncio.run(receiver.replay_pending()) == 0

    assert [
        (record.levelno, record.getMessage())
        for record in caplog.records
        if record.name == _DELIVERY_LOGGER and record.levelno >= logging.WARNING
    ] == [
        (
            logging.WARNING,
            "GitHub webhook delivery undecodable-1 failure line: action fell back to - "
            "after JSONDecodeError",
        ),
        (
            logging.WARNING,
            "GitHub webhook delivery undecodable-1 event=pull_request action=- "
            "failed stage=dispatch category=github_request error=ConnectError",
        ),
        (logging.ERROR, "GitHub webhook projection failed for delivery undecodable-1"),
    ]
    (sweep_record,) = _sweep_failure_records(caplog)
    assert sweep_record.exc_info is None
    assert vars(sweep_record)["error_type"] == httpx.ConnectError.__name__
    row = uow.rows["undecodable-1"]
    assert row.claim_token is None
    assert row.retry_after == _NOW + timedelta(seconds=30)


_PAYLOAD_SENTINEL = "SENTINEL-t72-payload-value"
_DISPATCH_LOGGER = "app.modules.integrations.webhooks.api.dispatch"


class UnreachableTypedDispatcher:
    """The typed dispatcher behind the real adapter: an invalid payload never reaches it."""

    async def execute(self, delivery: GitHubDispatchEvent) -> InstallationDeliveryDispatchResult:
        raise AssertionError("an invalid payload reached typed dispatch")


def _replay_invalid_receipt(
    receipt: WebhookReceipt, caplog: pytest.LogCaptureFixture
) -> FakeReceiptUnitOfWork:
    uow = FakeReceiptUnitOfWork()
    receiver = ReceiveGitHubDelivery(
        uow_factory=lambda: uow,
        dispatcher=GitHubWebhookDispatchAdapter(UnreachableTypedDispatcher()),
        now=lambda: _NOW,
    )
    asyncio.run(receiver.execute(receipt))
    with caplog.at_level(logging.INFO, logger=_DELIVERY_LOGGER):
        assert asyncio.run(receiver.replay_pending()) == 1
    return uow


def test_invalid_labeled_payload_logs_its_action_and_reason_without_payload_values(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A rejected ``labeled`` delivery still says which action it was and why no Run (api#72)."""
    # A labeled payload that reaches typed dispatch, except for its empty label.
    payload = json.loads(_pull_request_receipt("invalid-label-1").payload_json)
    payload["label"] = {}
    payload["pull_request"]["title"] = _PAYLOAD_SENTINEL
    receipt = VerifiedGitHubDelivery("invalid-label-1", "pull_request", payload).to_receipt()

    uow = _replay_invalid_receipt(receipt, caplog)

    assert _outcome_lines(caplog) == [
        "GitHub webhook delivery invalid-label-1 event=pull_request "
        "status=ignored_invalid_event detail=action=labeled invalid_payload fields=label.name"
    ]
    assert all(_PAYLOAD_SENTINEL not in record.getMessage() for record in caplog.records)
    assert _PAYLOAD_SENTINEL not in caplog.text
    row = uow.rows["invalid-label-1"]
    assert row.projected is True
    assert row.retry_after is None
    assert row.projection_attempt_count == 0


def _synchronize_with_unparsable_number() -> str:
    """A ``synchronize`` payload that is valid except for a string ``pull_request.number``."""
    payload = json.loads(_pull_request_receipt("payload", "synchronize").payload_json)
    payload["pull_request"]["number"] = _PAYLOAD_SENTINEL
    return json.dumps(payload)


@pytest.mark.parametrize(
    ("event_name", "payload_json", "detail"),
    [
        (
            "pull_request",
            _synchronize_with_unparsable_number(),
            "action=synchronize invalid_payload fields=pull_request.number",
        ),
        ("pull_request", "not json", "invalid_payload"),
        ("pull_request", "[]", "invalid_payload"),
        (
            "check_suite",
            json.dumps({"action": "completed", "installation": {"id": 17}}),
            "invalid_payload fields=repository,check_suite",
        ),
    ],
    ids=["synchronize-broken-pull-request", "undecodable", "json-list", "check-suite"],
)
def test_every_invalid_payload_logs_the_invalid_payload_reason(
    caplog: pytest.LogCaptureFixture, event_name: str, payload_json: str, detail: str
) -> None:
    uow = _replay_invalid_receipt(WebhookReceipt("invalid-1", event_name, payload_json), caplog)

    assert _outcome_lines(caplog) == [
        f"GitHub webhook delivery invalid-1 event={event_name} "
        f"status=ignored_invalid_event detail={detail}"
    ]
    assert [record for record in caplog.records if record.levelno >= logging.WARNING] == []
    assert _PAYLOAD_SENTINEL not in caplog.text
    row = uow.rows["invalid-1"]
    assert row.projected is True
    assert row.retry_after is None
    assert row.projection_attempt_count == 0


def _pull_request_payload(action: str = "labeled") -> dict[str, object]:
    payload = json.loads(_pull_request_receipt("payload", action).payload_json)
    assert isinstance(payload, dict)
    return payload


def _without_label() -> dict[str, object]:
    payload = _pull_request_payload()
    del payload["label"]
    return payload


def _mismatched_number() -> dict[str, object]:
    return {**_pull_request_payload(), "number": 8}


@pytest.mark.parametrize(
    ("event_name", "payload", "detail"),
    [
        ("pull_request", _without_label(), "action=labeled invalid_payload fields=label"),
        ("pull_request", _mismatched_number(), "action=labeled invalid_payload fields=number"),
        (
            "check_suite",
            {
                "action": "completed",
                "installation": {"id": 17},
                "repository": {"id": 101},
                "check_suite": {"head_sha": _PAYLOAD_SENTINEL},
            },
            "invalid_payload fields=check_suite.head_sha",
        ),
    ],
    ids=["label-missing", "number-mismatch", "check-suite-head-sha"],
)
def test_invalid_payload_names_its_failing_fields_never_their_values(
    caplog: pytest.LogCaptureFixture, event_name: str, payload: dict[str, object], detail: str
) -> None:
    payload = {**payload, "sender": {"type": "User", "login": _PAYLOAD_SENTINEL}}
    receipt = VerifiedGitHubDelivery("fields-1", event_name, payload).to_receipt()

    uow = _replay_invalid_receipt(receipt, caplog)

    assert _outcome_lines(caplog) == [
        f"GitHub webhook delivery fields-1 event={event_name} "
        f"status=ignored_invalid_event detail={detail}"
    ]
    assert all(_PAYLOAD_SENTINEL not in record.getMessage() for record in caplog.records)
    assert _PAYLOAD_SENTINEL not in caplog.text
    assert uow.rows["fields-1"].projected is True


def test_a_field_name_that_is_not_an_identifier_is_logged_as_a_question_mark(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    def parse_with_odd_locations(event_name: str, payload: Mapping[str, object]) -> CiTriggerEvent:
        raise ValidationError.from_exception_data(
            "check_suite",
            [
                {
                    "type": "missing",
                    "loc": ("check_suite", f"{_PAYLOAD_SENTINEL}\n", 0),
                    "input": {},
                },
                {"type": "missing", "loc": ("check_suite", "pull-requests", 0), "input": {}},
                {"type": "missing", "loc": ("check_suite", "x" * 65), "input": {}},
                {"type": "missing", "loc": ("check_suite", "_app_1"), "input": {}},
            ],
        )

    monkeypatch.setattr(
        "app.modules.integrations.webhooks.api.dispatch.parse_ci_event", parse_with_odd_locations
    )
    receipt = VerifiedGitHubDelivery(
        "odd-fields-1", "check_suite", {"action": "completed"}
    ).to_receipt()

    _replay_invalid_receipt(receipt, caplog)

    # The two odd segments of the same shape collapse into one location.
    assert _outcome_lines(caplog) == [
        "GitHub webhook delivery odd-fields-1 event=check_suite status=ignored_invalid_event "
        "detail=invalid_payload fields=check_suite.?.0,check_suite.?,check_suite._app_1"
    ]
    assert _PAYLOAD_SENTINEL not in caplog.text


def test_invalid_payload_logs_each_field_path_once_and_at_most_ten(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Twelve locations, the fourth a repeat of the second: eleven paths, ten of them shown.
    locations: tuple[tuple[int | str, ...], ...] = (
        ("check_suite", "head_sha"),
        ("check_suite", "head_branch"),
        ("check_suite", "id"),
        ("check_suite", "head_branch"),
        ("check_suite", "app", "id"),
        ("check_suite", "pull_requests", 0, "number"),
        ("check_suite", "pull_requests", 1, "number"),
        ("repository", "id"),
        ("repository", "full_name"),
        ("installation", "id"),
        ("check_suite", "status"),
        ("check_suite", "conclusion"),
    )

    def parse_with_many_locations(event_name: str, payload: Mapping[str, object]) -> CiTriggerEvent:
        raise ValidationError.from_exception_data(
            "check_suite",
            [{"type": "missing", "loc": location, "input": {}} for location in locations],
        )

    monkeypatch.setattr(
        "app.modules.integrations.webhooks.api.dispatch.parse_ci_event", parse_with_many_locations
    )
    receipt = VerifiedGitHubDelivery(
        "many-fields-1", "check_suite", {"action": "completed"}
    ).to_receipt()

    _replay_invalid_receipt(receipt, caplog)

    assert _outcome_lines(caplog) == [
        "GitHub webhook delivery many-fields-1 event=check_suite status=ignored_invalid_event "
        "detail=invalid_payload fields=check_suite.head_sha,check_suite.head_branch,"
        "check_suite.id,check_suite.app.id,check_suite.pull_requests.0.number,"
        "check_suite.pull_requests.1.number,repository.id,repository.full_name,installation.id,"
        "check_suite.status,+1"
    ]


def test_an_empty_field_location_is_logged_as_a_question_mark(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A model-level error carries no location at all.
    def parse_with_empty_location(event_name: str, payload: Mapping[str, object]) -> CiTriggerEvent:
        raise ValidationError.from_exception_data(
            "check_suite",
            [
                {"type": "missing", "loc": (), "input": {}},
                {"type": "missing", "loc": ("check_suite", "head_sha"), "input": {}},
            ],
        )

    monkeypatch.setattr(
        "app.modules.integrations.webhooks.api.dispatch.parse_ci_event", parse_with_empty_location
    )
    receipt = VerifiedGitHubDelivery(
        "empty-loc-1", "check_suite", {"action": "completed"}
    ).to_receipt()

    _replay_invalid_receipt(receipt, caplog)

    assert _outcome_lines(caplog) == [
        "GitHub webhook delivery empty-loc-1 event=check_suite status=ignored_invalid_event "
        "detail=invalid_payload fields=?,check_suite.head_sha"
    ]


def test_a_rejection_that_names_no_field_is_logged_as_plain_invalid_payload(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    def parse_with_plain_error(event_name: str, payload: Mapping[str, object]) -> CiTriggerEvent:
        raise ValueError(f"unexpected {_PAYLOAD_SENTINEL}")

    monkeypatch.setattr(
        "app.modules.integrations.webhooks.api.dispatch.parse_ci_event", parse_with_plain_error
    )
    receipt = VerifiedGitHubDelivery(
        "plain-1", "workflow_run", {"action": "completed"}
    ).to_receipt()

    _replay_invalid_receipt(receipt, caplog)

    assert _outcome_lines(caplog) == [
        "GitHub webhook delivery plain-1 event=workflow_run status=ignored_invalid_event "
        "detail=invalid_payload"
    ]
    assert _PAYLOAD_SENTINEL not in caplog.text


def test_invalid_installation_payload_logs_the_reason_next_to_the_field_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    receipt = VerifiedGitHubDelivery(
        "invalid-installation-1",
        "installation",
        {"action": "created", "installation": {"id": 17}},
    ).to_receipt()

    uow = _replay_invalid_receipt(receipt, caplog)

    assert _outcome_lines(caplog) == [
        "GitHub webhook delivery invalid-installation-1 event=installation "
        "status=ignored_invalid_event detail=invalid_payload"
    ]
    # The failing field names stay in the dispatch warning (api#71), unchanged.
    (warning,) = [
        record
        for record in caplog.records
        if record.name == _DISPATCH_LOGGER and record.levelno == logging.WARNING
    ]
    assert warning.getMessage().startswith(
        "Ignoring invalid GitHub installation event: delivery_id=invalid-installation-1 "
        "event=installation action=created installation_id=17 error_count="
    )
    row = uow.rows["invalid-installation-1"]
    assert row.projected is True
    assert row.retry_after is None
    assert row.projection_attempt_count == 0


_EVENT_NAMES_AS_LOGGED = [
    ("pull_request", "pull_request"),
    ("pull_request\nx", "-"),
    ("Pull Request", "-"),
]


@pytest.mark.parametrize(("event_name", "logged"), _EVENT_NAMES_AS_LOGGED)
def test_outcome_line_logs_the_event_name_only_as_a_plain_token(
    caplog: pytest.LogCaptureFixture, event_name: str, logged: str
) -> None:
    uow = FakeReceiptUnitOfWork()
    dispatcher = FakeDispatcher()
    receiver = ReceiveGitHubDelivery(
        uow_factory=lambda: uow, dispatcher=dispatcher, now=lambda: _NOW
    )
    asyncio.run(receiver.execute(WebhookReceipt("event-1", event_name, "{}")))
    with caplog.at_level(logging.INFO, logger=_DELIVERY_LOGGER):
        assert asyncio.run(receiver.replay_pending()) == 1

    assert _outcome_lines(caplog) == [
        f"GitHub webhook delivery event-1 event={logged} status=onboarded detail=-"
    ]
    # Only the log is restricted: the receipt is stored and dispatched as received.
    assert [delivery.event_name for delivery in dispatcher.deliveries] == [event_name]


@pytest.mark.parametrize(("event_name", "logged"), _EVENT_NAMES_AS_LOGGED)
def test_failure_line_logs_the_event_name_only_as_a_plain_token(
    caplog: pytest.LogCaptureFixture, event_name: str, logged: str
) -> None:
    class RaisingDispatcher:
        async def execute(self, delivery: WebhookReceipt) -> InstallationDeliveryDispatchResult:
            raise httpx.ConnectError("refused")

    uow = FakeReceiptUnitOfWork()
    receiver = ReceiveGitHubDelivery(
        uow_factory=lambda: uow,
        dispatcher=RaisingDispatcher(),
        now=lambda: _NOW,
        action_of=action_of,
        classify_failure=classify_failure,
    )
    asyncio.run(receiver.execute(WebhookReceipt("event-2", event_name, '{"action": "labeled"}')))
    with caplog.at_level(logging.INFO, logger=_DELIVERY_LOGGER):
        assert asyncio.run(receiver.replay_pending()) == 0

    assert _failure_lines(caplog) == [
        f"GitHub webhook delivery event-2 event={logged} action=labeled "
        "failed stage=dispatch category=github_request error=ConnectError"
    ]
    assert uow.rows["event-2"].delivery.event_name == event_name


def test_failure_line_has_no_action_without_a_reader(caplog: pytest.LogCaptureFixture) -> None:
    uow = FakeReceiptUnitOfWork()
    receiver = ReceiveGitHubDelivery(
        uow_factory=lambda: uow,
        dispatcher=GitHubWebhookDispatchAdapter(FailingTypedDispatcher(RuntimeError("boom"))),
        now=lambda: _NOW,
    )
    asyncio.run(receiver.execute(_pull_request_receipt("bare-1")))
    with caplog.at_level(logging.INFO, logger=_DELIVERY_LOGGER):
        assert asyncio.run(receiver.replay_pending()) == 0

    assert _failure_lines(caplog) == [
        "GitHub webhook delivery bare-1 event=pull_request action=- "
        "failed stage=dispatch category=internal error=RuntimeError"
    ]


@pytest.mark.parametrize(
    ("typed", "dispatch_timeout_seconds", "category", "error_name"),
    [
        pytest.param(
            FailingTypedDispatcher(httpx.ConnectError("refused")),
            1.0,
            "internal",
            "ConnectError",
            id="library-error",
        ),
        pytest.param(FailingTypedDispatcher(), 0.001, "timeout", "TimeoutError", id="timeout"),
        pytest.param(
            FailingTypedDispatcher(TimeoutError("slow")),
            1.0,
            "timeout",
            "TimeoutError",
            id="builtin-timeout",
        ),
    ],
)
def test_without_a_classifier_only_a_timeout_has_a_category_of_its_own(
    caplog: pytest.LogCaptureFixture,
    typed: FailingTypedDispatcher,
    dispatch_timeout_seconds: float,
    category: str,
    error_name: str,
) -> None:
    uow = _dispatch_failure_run(
        _pull_request_receipt("unclassified-1"),
        typed,
        caplog,
        dispatch_timeout_seconds=dispatch_timeout_seconds,
        classifier=None,
    )

    assert _failure_lines(caplog) == [
        "GitHub webhook delivery unclassified-1 event=pull_request action=labeled "
        f"failed stage=dispatch category={category} error={error_name}"
    ]
    assert _fallback_records(caplog) == []
    assert uow.rows["unclassified-1"].retry_after == _NOW + timedelta(seconds=30)


def test_an_error_the_classifier_does_not_recognise_is_internal_and_still_released_for_retry(
    caplog: pytest.LogCaptureFixture,
) -> None:
    class UnrecognisedError(Exception):
        """An error from none of the libraries the classifier knows."""

    uow = _dispatch_failure_run(
        _pull_request_receipt("odd-1"), FailingTypedDispatcher(UnrecognisedError("boom")), caplog
    )

    assert _failure_lines(caplog) == [
        "GitHub webhook delivery odd-1 event=pull_request action=labeled "
        "failed stage=dispatch category=internal error=UnrecognisedError"
    ]
    (sweep_record,) = _sweep_failure_records(caplog)
    assert sweep_record.exc_info is None
    assert vars(sweep_record)["error_type"] == UnrecognisedError.__name__
    assert uow.rows["odd-1"].retry_after == _NOW + timedelta(seconds=30)


def test_a_failing_classifier_still_writes_the_line_with_the_internal_category(
    caplog: pytest.LogCaptureFixture,
) -> None:
    def broken_classifier(exc: BaseException) -> FailureCategory:
        raise LookupError(f"classifier bug {_SECRET_IN_MESSAGE}")

    uow = _dispatch_failure_run(
        _pull_request_receipt("mask-1"),
        FailingTypedDispatcher(httpx.ConnectError("refused")),
        caplog,
        classifier=broken_classifier,
    )

    # The diagnostic record, then the failure line, then the sweep's traceback record.
    assert [
        (record.levelno, record.getMessage())
        for record in caplog.records
        if record.name == _DELIVERY_LOGGER and record.levelno >= logging.WARNING
    ] == [
        (
            logging.WARNING,
            "GitHub webhook delivery mask-1 failure line: category fell back to internal "
            "after LookupError",
        ),
        (
            logging.WARNING,
            "GitHub webhook delivery mask-1 event=pull_request action=labeled "
            "failed stage=dispatch category=internal error=ConnectError",
        ),
        (logging.ERROR, "GitHub webhook projection failed for delivery mask-1"),
    ]
    (fallback,) = _fallback_records(caplog)
    assert fallback.exc_info is None
    assert all(_SECRET_IN_MESSAGE not in record.getMessage() for record in caplog.records)
    (sweep_record,) = _sweep_failure_records(caplog)
    assert sweep_record.exc_info is None
    assert vars(sweep_record)["error_type"] == httpx.ConnectError.__name__
    row = uow.rows["mask-1"]
    assert row.claim_token is None
    assert row.retry_after == _NOW + timedelta(seconds=30)


def test_an_outcome_that_cannot_be_rendered_still_leaves_the_finalize_line(
    caplog: pytest.LogCaptureFixture,
) -> None:
    @dataclass
    class MarkFailsUnitOfWork(FakeReceiptUnitOfWork):
        async def mark_projected(self, delivery_id: str, token: UUID, at: datetime) -> None:
            raise OperationalError("UPDATE", {}, Exception("connection lost"))

    class UnrenderableResult:
        """A dispatch result whose detail cannot be read back for the failure line."""

        status = InstallationDeliveryDispatchStatus.PROJECTED_PR

        @property
        def detail(self) -> str:
            raise ValueError(f"detail bug {_SECRET_IN_MESSAGE}")

    class Dispatcher:
        async def execute(self, delivery: WebhookReceipt) -> InstallationDeliveryDispatchResult:
            return cast(InstallationDeliveryDispatchResult, UnrenderableResult())

    uow = MarkFailsUnitOfWork()
    receiver = ReceiveGitHubDelivery(
        uow_factory=lambda: uow,
        dispatcher=Dispatcher(),
        now=lambda: _NOW,
        action_of=action_of,
        classify_failure=classify_failure,
    )
    asyncio.run(receiver.execute(_pull_request_receipt("outcome-1")))
    with caplog.at_level(logging.INFO, logger=_DELIVERY_LOGGER):
        assert asyncio.run(receiver.replay_pending()) == 0

    assert _failure_lines(caplog) == [
        "GitHub webhook delivery outcome-1 event=pull_request action=labeled "
        "failed stage=finalize category=database error=OperationalError outcome=-"
    ]
    (fallback,) = _fallback_records(caplog)
    assert fallback.getMessage() == (
        "GitHub webhook delivery outcome-1 failure line: outcome fell back to - after ValueError"
    )
    assert fallback.exc_info is None
    assert all(_SECRET_IN_MESSAGE not in record.getMessage() for record in caplog.records)
    # The sweep keeps the exception class, without rendering its message/traceback.
    (sweep_record,) = _sweep_failure_records(caplog)
    assert sweep_record.exc_info is None
    assert vars(sweep_record)["error_type"] == OperationalError.__name__


def test_cancelled_dispatch_is_neither_logged_as_a_failure_nor_swallowed(
    caplog: pytest.LogCaptureFixture,
) -> None:
    uow = FakeReceiptUnitOfWork()
    receiver = ReceiveGitHubDelivery(
        uow_factory=lambda: uow,
        dispatcher=GitHubWebhookDispatchAdapter(FailingTypedDispatcher(asyncio.CancelledError())),
        now=lambda: _NOW,
        action_of=action_of,
    )
    asyncio.run(receiver.execute(_pull_request_receipt("cancel-1")))

    with (
        caplog.at_level(logging.INFO, logger=_DELIVERY_LOGGER),
        pytest.raises(asyncio.CancelledError),
    ):
        asyncio.run(receiver.replay_pending())

    assert _failure_lines(caplog) == []
    assert _sweep_failure_records(caplog) == []


@pytest.mark.parametrize("commit_fails", [True, False])
def test_final_deferral_is_logged_only_once_the_receipt_is_committed(
    caplog: pytest.LogCaptureFixture, commit_fails: bool
) -> None:
    @dataclass
    class CommitUnitOfWork(FakeReceiptUnitOfWork):
        dispatched: bool = False

        async def commit(self) -> None:
            if commit_fails and self.dispatched:
                raise OperationalError("COMMIT", {}, Exception("connection lost"))
            await super().commit()

    receipt = _pull_request_receipt("defer-1")
    # Two attempts are used up: this one is the third, so the deferral is final.
    uow = CommitUnitOfWork(
        rows={"defer-1": FakeClaimedReceipt(receipt, projection_attempt_count=2)}
    )

    class Dispatcher:
        async def execute(self, delivery: WebhookReceipt) -> InstallationDeliveryDispatchResult:
            uow.dispatched = True
            return InstallationDeliveryDispatchResult(
                InstallationDeliveryDispatchStatus.IGNORED_UNKNOWN_REPOSITORY, "unknown_repository"
            )

    receiver = ReceiveGitHubDelivery(
        uow_factory=lambda: uow,
        dispatcher=Dispatcher(),
        now=lambda: _NOW,
        action_of=action_of,
        classify_failure=classify_failure,
    )
    with caplog.at_level(logging.INFO, logger=_DELIVERY_LOGGER):
        assert asyncio.run(receiver.replay_pending()) == (0 if commit_fails else 1)

    warnings = [
        record.getMessage() for record in caplog.records if record.levelno == logging.WARNING
    ]
    if commit_fails:
        assert warnings == [
            "GitHub webhook delivery defer-1 event=pull_request action=labeled "
            "failed stage=finalize category=database error=OperationalError "
            "outcome=ignored_unknown_repository detail=unknown_repository"
        ]
    else:
        assert warnings == [
            "GitHub webhook delivery defer-1 deferred after its last attempt: "
            "ignored_unknown_repository"
        ]


@pytest.mark.parametrize(
    ("payload_json", "expected"),
    [
        ('{"action": "labeled"}', "labeled"),
        ('{"action": "ready_for_review"}', "ready_for_review"),
        ('{"action": "' + "a" * 40 + '"}', "a" * 40),
        ('{"action": "' + "a" * 41 + '"}', None),
        ('{"action": "labeled\\nFAKE LINE"}', None),
        ('{"action": "labeled\\n"}', None),
        ('{"action": "Labeled"}', None),
        ('{"action": "labeled pr"}', None),
        ('{"action": ""}', None),
        ('{"action": 7}', None),
        ('{"action": null}', None),
        ('{"action": ["labeled"]}', None),
        ("{}", None),
        ('["labeled"]', None),
        ('"labeled"', None),
    ],
)
def test_action_reader_returns_only_a_plain_token(payload_json: str, expected: str | None) -> None:
    receipt = WebhookReceipt("reader", "pull_request", payload_json)

    assert action_of(receipt) == expected


@pytest.mark.parametrize(
    ("payload_json", "error", "message"),
    [
        ("not json", json.JSONDecodeError, "Expecting value"),
        ("", json.JSONDecodeError, "Expecting value"),
        pytest.param(
            "[" * 100_000, RecursionError, "maximum recursion depth exceeded", id="deeply-nested"
        ),
    ],
)
def test_action_reader_raises_for_a_payload_that_cannot_be_decoded(
    payload_json: str, error: type[Exception], message: str
) -> None:
    receipt = WebhookReceipt("reader", "pull_request", payload_json)

    with pytest.raises(error, match=message):
        action_of(receipt)


@pytest.mark.parametrize(
    ("error", "member", "logged"),
    [
        (httpx.ConnectError("refused"), FailureCategory.GITHUB_REQUEST, "github_request"),
        (httpx.ReadTimeout("slow"), FailureCategory.GITHUB_REQUEST, "github_request"),
        (
            httpx.HTTPStatusError(
                "500", request=httpx.Request("GET", "https://x"), response=httpx.Response(500)
            ),
            FailureCategory.GITHUB_REQUEST,
            "github_request",
        ),
        (
            OperationalError("SELECT 1", {}, Exception("lost")),
            FailureCategory.DATABASE,
            "database",
        ),
        (PoolTimeoutError("QueuePool limit reached"), FailureCategory.DATABASE, "database"),
        (psycopg.OperationalError("lost"), FailureCategory.DATABASE, "database"),
        (ValueError("bad"), FailureCategory.INTERNAL, "internal"),
        (KeyError("missing"), FailureCategory.INTERNAL, "internal"),
        # The dispatch timeout is the application's own category, not a library family.
        (TimeoutError(), FailureCategory.INTERNAL, "internal"),
    ],
)
def test_failure_classifier_names_the_library_family_of_the_error(
    error: BaseException, member: FailureCategory, logged: str
) -> None:
    category = classify_failure(error)

    assert category is member
    assert f"{category}" == logged


def test_commit_failure_cannot_acknowledge_receipt() -> None:
    body = b'{"action":"opened"}'

    @dataclass
    class FailingCommitUnitOfWork(FakeReceiptUnitOfWork):
        async def commit(self) -> None:
            raise RuntimeError("database commit failed")

        async def __aexit__(
            self,
            exc_type: type[BaseException] | None,
            exc: BaseException | None,
            traceback: TracebackType | None,
        ) -> None:
            self.rows.clear()

    uow = FailingCommitUnitOfWork()
    app.dependency_overrides[get_github_webhook_secret] = lambda: _SECRET
    app.dependency_overrides[get_github_webhook_receipt_uow_factory] = lambda: lambda: uow
    try:
        response = TestClient(app, raise_server_exceptions=False).post(
            "/webhooks/github", content=body, headers=_headers(body)
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 500
    assert uow.rows == {}


def test_worker_claim_commit_finishes_before_dispatch() -> None:
    now = datetime(2026, 9, 28, tzinfo=UTC)
    uow = FakeReceiptUnitOfWork()
    delivery = VerifiedGitHubDelivery("ordered-1", "ping", {"zen": "hello"})

    class AssertingDispatcher:
        async def execute(self, delivery: WebhookReceipt) -> InstallationDeliveryDispatchResult:
            assert uow.commits == 2
            assert uow.rows["ordered-1"].claim_token is not None
            return InstallationDeliveryDispatchResult(InstallationDeliveryDispatchStatus.ONBOARDED)

    receiver = ReceiveGitHubDelivery(
        uow_factory=lambda: uow, dispatcher=AssertingDispatcher(), now=lambda: now
    )

    assert (
        asyncio.run(receiver.execute(delivery.to_receipt()))
        == InstallationDeliveryDispatchStatus.PENDING
    )
    assert asyncio.run(receiver.replay_pending()) == 1
    assert uow.commits == 3


def test_invalid_installation_event_logs_its_action_only_as_a_plain_token(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # The envelope fails on the installation id, so a free-text action reaches the warning.
    receipt = VerifiedGitHubDelivery(
        "invalid-installation-2",
        "installation_repositories",
        {"action": "zq7 status=projected", "installation": {"id": 0}},
    ).to_receipt()

    _replay_invalid_receipt(receipt, caplog)

    (warning,) = [
        record.getMessage()
        for record in caplog.records
        if record.name == _DISPATCH_LOGGER and record.levelno == logging.WARNING
    ]
    assert warning.startswith(
        "Ignoring invalid GitHub installation event: delivery_id=invalid-installation-2 "
        "event=installation_repositories action=- installation_id=0 error_count="
    )
    assert "zq7" not in caplog.text
