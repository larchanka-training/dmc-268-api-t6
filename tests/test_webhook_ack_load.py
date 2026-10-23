"""The webhook ACK load script sends signed pings the real route stores (#163)."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import importlib.util
import json
import sys
from collections.abc import Callable, Iterator
from pathlib import Path
from types import ModuleType, TracebackType
from typing import Any, NoReturn, Self
from uuid import UUID

import httpx
import pytest

from app.bootstrap.reviews_api import get_github_webhook_receipt_uow_factory
from app.main import app, get_github_webhook_secret
from app.modules.integrations.webhooks.api.dispatch import GitHubWebhookDispatchAdapter
from app.modules.integrations.webhooks.application.github_installation_dispatch import (
    GitHubInstallationDeliveryDispatcher,
    InstallationDeliveryDispatchResult,
    InstallationDeliveryDispatchStatus,
)
from app.modules.integrations.webhooks.application.receive_github_delivery import WebhookReceipt
from app.modules.repositories.application.installation_repositories import (
    InstallationRepositoriesEvent,
)
from app.modules.repositories.application.onboard_repository import OnboardingResult
from app.modules.reviews.application.project_github_pull_request import (
    PullRequestEvent,
    PullRequestLabelEvent,
    PullRequestProjectionStatus,
)
from app.modules.reviews.application.trigger_from_delivery import CiTriggerEvent, RunTriggerOutcome

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/webhook_ack_load.py"
_SECRET = "ack-load-test-secret"
_URL = "http://test/webhooks/github"


@pytest.fixture(scope="module")
def load() -> Iterator[ModuleType]:
    spec = importlib.util.spec_from_file_location("webhook_ack_load", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
        yield module
    finally:
        sys.modules.pop(spec.name, None)


class MemoryReceipts:
    """Only what the HTTP route uses: save in one unit of work, then commit."""

    def __init__(self) -> None:
        self.rows: dict[str, WebhookReceipt] = {}
        self.commits = 0

    @property
    def receipts(self) -> MemoryReceipts:
        return self

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        return None

    async def commit(self) -> None:
        self.commits += 1

    async def rollback(self) -> None:
        return None

    async def save(self, delivery: WebhookReceipt) -> bool:
        if delivery.delivery_id in self.rows:
            return False
        self.rows[delivery.delivery_id] = delivery
        return True


@pytest.fixture
def receipts() -> Iterator[MemoryReceipts]:
    store = MemoryReceipts()
    saved = app.dependency_overrides.copy()
    app.dependency_overrides[get_github_webhook_secret] = lambda: _SECRET
    app.dependency_overrides[get_github_webhook_receipt_uow_factory] = lambda: lambda: store
    try:
        yield store
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(saved)


def _app_transport() -> httpx.ASGITransport:
    return httpx.ASGITransport(app)


def _run(load: ModuleType, transport: httpx.AsyncBaseTransport, **values: object) -> Any:
    arguments: dict[str, object] = {
        "url": _URL,
        "secret": _SECRET,
        "count": 20,
        "concurrency": 1,
        "body_bytes": 1024,
        "timeout": 5.0,
        "transport": transport,
    }
    arguments.update(values)
    return asyncio.run(load.run(**arguments))


def _accepted() -> httpx.Response:
    return httpx.Response(202, json={"status": "pending"})


def _numbered(answer: Callable[[int], httpx.Response]) -> httpx.MockTransport:
    """Request 1 is the warm-up (concurrency 1); ``answer`` gets the timed number from 1."""
    seen = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal seen
        seen += 1
        return _accepted() if seen == 1 else answer(seen - 1)

    return httpx.MockTransport(handler)


def _recording(calls: list[httpx.Request]) -> httpx.MockTransport:
    def accept(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return _accepted()

    return httpx.MockTransport(accept)


# --- the real route and the worker's dispatch ------------------------------------------------


def test_every_delivery_is_a_signed_ping_the_route_stores(
    load: ModuleType, receipts: MemoryReceipts
) -> None:
    report = _run(load, _app_transport(), count=12, concurrency=3, body_bytes=4096)

    # 3 warm-up (one per connection) + 12 timed deliveries, each a fresh receipt.
    assert report.statuses == {"202": 15}
    assert (report.count, report.warmup, report.sent) == (12, 3, 15)
    assert len(receipts.rows) == 15 and receipts.commits == 15
    for delivery_id, receipt in receipts.rows.items():
        assert delivery_id.startswith("loadtest-") and len(delivery_id) == 45
        assert receipt.event_name == "ping"
        payload = json.loads(receipt.payload_json)
        assert "installation" not in payload
        assert payload["zen"] == "Load test, ignore."
        assert len(payload["padding"]) == 25 and len(payload["fill"]) == 12
    assert report.target_host == "test"
    assert report.aborted is None and report.all_accepted


class NoResolver:
    async def find_github_installation_id(self, external_id: int) -> UUID | None:
        _untouched("the installation resolver")


class NoOnboarding:
    async def execute(
        self, *, provider_installation_id: UUID, event: InstallationRepositoriesEvent
    ) -> tuple[OnboardingResult, ...]:
        _untouched("onboarding")


class NoProjector:
    async def execute(self, event: PullRequestEvent) -> PullRequestProjectionStatus:
        _untouched("the PR projector")


class NoLabelProjector:
    async def execute(self, event: PullRequestLabelEvent) -> PullRequestProjectionStatus:
        _untouched("the label projector")


class NoRunTrigger:
    async def on_pr(self, event: PullRequestEvent) -> RunTriggerOutcome:
        _untouched("the Run trigger")

    async def on_label(self, event: PullRequestLabelEvent) -> RunTriggerOutcome:
        _untouched("the Run trigger")

    async def on_ci(self, event: CiTriggerEvent) -> RunTriggerOutcome:
        _untouched("the Run trigger")


class NoRulesRefresh:
    async def execute(self, installation_external_id: int, repository_external_id: int) -> bool:
        _untouched("the rules refresh")


class NoIndexRefresh:
    async def execute(
        self, installation_external_id: int, repository_external_id: int, commit_sha: str | None
    ) -> bool:
        _untouched("the index refresh")


def _untouched(name: str) -> NoReturn:
    raise AssertionError(f"a ping delivery must not reach {name}")


def test_a_ping_receipt_ends_ignored_without_any_lookup_or_run(load: ModuleType) -> None:
    body, headers = load.build_ping(_SECRET, "loadtest-dispatch", 16384)
    adapter = GitHubWebhookDispatchAdapter(
        GitHubInstallationDeliveryDispatcher(
            resolver=NoResolver(),
            onboarding=NoOnboarding(),
            pull_request_projector=NoProjector(),
            label_intent_projector=NoLabelProjector(),
            run_trigger=NoRunTrigger(),
            rules_refresh=NoRulesRefresh(),
            index_refresh=NoIndexRefresh(),
        )
    )
    receipt = WebhookReceipt("loadtest-dispatch", headers["X-GitHub-Event"], body.decode())

    result = asyncio.run(adapter.execute(receipt))

    assert result == InstallationDeliveryDispatchResult(
        InstallationDeliveryDispatchStatus.IGNORED_IRRELEVANT_EVENT, None
    )


# --- body and concurrency --------------------------------------------------------------------


def test_ping_body_is_structured_padding_of_the_requested_size(load: ModuleType) -> None:
    body, headers = load.build_ping("s3cret", "loadtest-1", 16384)
    payload = json.loads(body)

    assert len(body) == 16384
    assert sorted(payload) == ["fill", "hook_id", "padding", "zen"]
    assert len(payload["padding"]) == 101 and payload["fill"] == "x" * 15
    assert payload["padding"][:2] == [
        {
            "id": 0,
            "path": "src/module_0/file_0.py",
            "status": "added",
            "additions": 0,
            "deletions": 0,
            "binary": True,
            "score": 0.0,
            "sha": None,
        },
        {
            "id": 1,
            "path": "src/module_1/file_1.py",
            "status": "modified",
            "additions": 7,
            "deletions": 3,
            "binary": False,
            "score": 0.062,
            "sha": "0000000000000000000000000000000000000001",
        },
    ]
    assert len(load.build_ping("s3cret", "loadtest-2", 65536)[0]) == 65536
    expected = hmac.new(b"s3cret", body, hashlib.sha256).hexdigest()
    assert headers == {
        "Content-Type": "application/json",
        "X-GitHub-Event": "ping",
        "X-GitHub-Delivery": "loadtest-1",
        "X-Hub-Signature-256": f"sha256={expected}",
    }


def test_warm_up_and_timed_requests_never_exceed_the_concurrency(load: ModuleType) -> None:
    in_flight = 0
    peaks: list[int] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal in_flight
        in_flight += 1
        peaks.append(in_flight)
        await asyncio.sleep(0.01)
        in_flight -= 1
        return _accepted()

    report = _run(load, httpx.MockTransport(handler), count=20, concurrency=4)

    assert report.statuses == {"202": 24}
    assert report.warmup == 4
    assert max(peaks) == 4
    # The four warm-up deliveries are in flight together: one per connection.
    assert peaks[:4] == [1, 2, 3, 4]


# --- arguments -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "argv,message",
    [
        (["--count", "1001"], "--count must be from 1 to 1000"),
        (["--count", "0"], "--count must be from 1 to 1000"),
        (["--concurrency", "11"], "--concurrency must be from 1 to 10"),
        (["--body-bytes", "65537"], "--body-bytes must be from 1 to 65536"),
        (["--url", "http://test/"], "--url must point at /webhooks/github"),
        (["--url", "http://test/webhooks/gitlab"], "--url must point at /webhooks/github"),
    ],
    ids=["count-over-cap", "count-zero", "concurrency-over-cap", "body-over-cap", "root", "gitlab"],
)
def test_invalid_arguments_exit_2_before_any_request(
    load: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    argv: list[str],
    message: str,
) -> None:
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", _SECRET)
    calls: list[httpx.Request] = []

    with pytest.raises(SystemExit) as exit_info:
        load.main(["--url", _URL, *argv], transport=_recording(calls))

    assert exit_info.value.code == 2
    assert message in capsys.readouterr().err
    assert calls == []


def test_the_caps_themselves_are_accepted(
    load: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", _SECRET)
    calls: list[httpx.Request] = []
    argv = ["--url", _URL, "--count", "1000", "--concurrency", "10", "--body-bytes", "65536"]

    assert load.main(argv, transport=_recording(calls)) == 0
    assert len(calls) == 1010
    assert len(calls[-1].content) == 65536


def test_missing_secret_exits_2_without_a_request(
    load: ModuleType, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("GITHUB_WEBHOOK_SECRET", raising=False)
    calls: list[httpx.Request] = []

    assert load.main(["--url", _URL], transport=_recording(calls)) == 2
    assert "GITHUB_WEBHOOK_SECRET is not set" in capsys.readouterr().err
    assert calls == []


# --- failures and fail-fast ------------------------------------------------------------------


def test_wrong_secret_stops_after_the_warm_up_and_writes_the_report(
    load: ModuleType,
    receipts: MemoryReceipts,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", "not-the-server-secret")
    report_path = tmp_path / "report.json"
    argv = ["--url", _URL, "--count", "300", "--report-json", str(report_path)]

    assert load.main(argv, transport=_app_transport()) == 1
    report = json.loads(report_path.read_text())
    # Only the 5 warm-up deliveries (default concurrency) are sent; no timed one follows.
    assert report["statuses"] == {"401": 5}
    assert (report["sent"], report["aborted"]) == (5, "a warm-up delivery did not get 202")
    assert report["latency_ms"] is None
    assert report["verdict"] == (
        "INVALID (5 of 5 deliveries failed): "
        "no 202 answer among the timed deliveries: no latency to compare with SD §13"
    )
    assert receipts.rows == {}


def test_one_failed_warm_up_delivery_stops_the_run(load: ModuleType) -> None:
    answers = iter([202, 202, 503])

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(next(answers, 202), json={})

    report = _run(load, httpx.MockTransport(handler), count=50, concurrency=3)

    assert report.statuses == {"202": 2, "503": 1}
    assert (report.sent, report.aborted) == (3, "a warm-up delivery did not get 202")


def test_five_failures_in_a_row_stop_the_timed_deliveries(load: ModuleType) -> None:
    report = _run(load, _numbered(lambda number: httpx.Response(500, json={})), count=100)

    assert report.statuses == {"202": 1, "500": 5}
    assert (report.sent, report.aborted) == (6, "5 timed deliveries in a row failed")


def test_five_failures_not_in_a_row_before_twenty_do_not_stop_the_run(load: ModuleType) -> None:
    def answer(number: int) -> httpx.Response:
        return httpx.Response(500, json={}) if number in (1, 2, 3, 4, 6) else _accepted()

    report = _run(load, _numbered(answer), count=19)

    # 4 in a row, one 202, then a fifth failure: the run resets the streak. 5 of 19 is over
    # 10 %, but the share rule waits for 20 timed deliveries.
    assert report.statuses == {"202": 15, "500": 5}
    assert report.aborted is None


def test_over_ten_percent_failed_after_twenty_stops_the_run(load: ModuleType) -> None:
    def answer(number: int) -> httpx.Response:
        return httpx.Response(500, json={}) if number % 8 == 0 else _accepted()

    report = _run(load, _numbered(answer), count=100)

    # Failures at 8, 16, 24: 2 of 20 is not over 10 %, 3 of 24 is.
    assert report.statuses == {"202": 22, "500": 3}
    assert (report.sent, report.aborted) == (25, "3 of 24 timed deliveries failed (over 10 %)")
    assert report.verdict.startswith("INVALID (3 of 25 deliveries failed): p95 ")


def test_slow_failures_are_counted_but_stay_out_of_the_latency(load: ModuleType) -> None:
    seen = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal seen
        seen += 1
        if seen in (9, 17):
            await asyncio.sleep(0.3)
            return httpx.Response(500, json={})
        return _accepted()

    report = _run(load, httpx.MockTransport(handler), count=20)

    # 2 failures in 20 timed deliveries: no stop, the run still fails, the 300 ms stay out.
    assert report.statuses == {"202": 19, "500": 2}
    assert report.aborted is None and not report.all_accepted
    assert report.latency_ms.max < 300


def test_a_transport_error_is_counted_and_fails_the_run(
    load: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    def answer(number: int) -> httpx.Response:
        if number == 3:
            raise httpx.ConnectError("connection reset")
        return _accepted()

    report = _run(load, _numbered(answer), count=10)

    assert report.statuses == {"202": 10, "transport_error": 1}
    assert report.verdict.startswith("INVALID (1 of 11 deliveries failed): p95 ")
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", _SECRET)
    argv = ["--url", _URL, "--count", "10", "--concurrency", "1"]
    assert load.main(argv, transport=_numbered(answer)) == 1


# --- report ----------------------------------------------------------------------------------


def test_report_json_has_the_run_parameters_labels_and_targets(
    load: ModuleType, receipts: MemoryReceipts, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", _SECRET)
    report_path = tmp_path / "report.json"
    window = "2026-10-24 10:00-10:15 UTC, axyi, #163 comment"
    argv = [
        "--url",
        "http://test:8080/webhooks/github",
        "--count",
        "4",
        "--concurrency",
        "2",
        "--body-bytes",
        "2048",
        "--environment",
        "staging",
        "--commit",
        "a" * 40,
        "--window",
        window,
        "--report-json",
        str(report_path),
    ]

    assert load.main(argv, transport=_app_transport()) == 0
    report = json.loads(report_path.read_text())
    labels = ("environment", "commit", "window", "target_host", "count", "warmup", "sent")
    assert {key: report[key] for key in labels} == {
        "environment": "staging",
        "commit": "a" * 40,
        "window": window,
        "target_host": "test:8080",
        "count": 4,
        "warmup": 2,
        "sent": 6,
    }
    assert (report["concurrency"], report["body_bytes"], report["aborted"]) == (2, 2048, None)
    assert report["statuses"] == {"202": 6}
    assert report["targets_ms"] == {"p95": 500.0, "p95_internal_goal": 200.0}
    assert sorted(report["latency_ms"]) == ["max", "p50", "p95", "p99"]
    assert report["started_at"].endswith("Z") and report["finished_at"].endswith("Z")
    assert report["verdict"].startswith("p95 ")


def test_nearest_rank_percentiles(load: ModuleType) -> None:
    samples = [float(value) for value in range(100, 0, -1)]

    assert load.percentile(samples, 50) == 50.0
    assert load.percentile(samples, 95) == 95.0
    assert load.percentile(samples, 99) == 99.0
    assert load.percentile([30.0, 10.0, 20.0], 50) == 20.0
    assert load.percentile([30.0, 10.0, 20.0], 95) == 30.0
    assert load.percentile([7.0], 99) == 7.0


_GOAL = "meets the SD §13 target (p95 < 500 ms) and the internal goal (200 ms)"
_TARGET = "meets the SD §13 target (p95 < 500 ms) but misses the internal goal (200 ms)"


@pytest.mark.parametrize(
    "p95,expected",
    [
        (199.9, f"p95 199.90 ms {_GOAL}"),
        (199.96, f"p95 199.96 ms {_GOAL}"),
        (199.999, f"p95 199.99 ms {_GOAL}"),
        (200.0, f"p95 200.00 ms {_TARGET}"),
        (499.96, f"p95 499.96 ms {_TARGET}"),
        (500.0, "p95 500.00 ms misses the SD §13 target (p95 < 500 ms)"),
    ],
    ids=[
        "within-goal",
        "just-below-goal",
        "cut-below-goal",
        "at-goal",
        "just-below-target",
        "at-target",
    ],
)
def test_verdict_compares_the_unrounded_p95_with_sd_13(
    load: ModuleType, p95: float, expected: str
) -> None:
    assert load.verdict(p95) == expected


def test_verdict_is_invalid_when_a_delivery_failed(load: ModuleType) -> None:
    assert load.verdict(120.5, failed=3, sent=205) == (
        f"INVALID (3 of 205 deliveries failed): p95 120.50 ms {_GOAL}"
    )


class StepClock:
    """``perf_counter`` for the script: every request takes exactly ``step`` seconds."""

    def __init__(self, step: float) -> None:
        self.step = step
        self.now = 0.0
        self.calls = 0

    def perf_counter(self) -> float:
        self.calls += 1
        if self.calls % 2 == 0:
            self.now += self.step
        return self.now


def test_the_run_compares_the_uncut_p95_with_the_target(
    load: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 499.996 ms rounds to 500.00 but is below the target: the verdict must say "meets".
    monkeypatch.setattr(load, "time", StepClock(0.499996))

    report = _run(load, _recording([]), count=3)

    assert report.latency_ms.p95 == 499.99
    assert report.verdict == f"p95 499.99 ms {_TARGET}"
