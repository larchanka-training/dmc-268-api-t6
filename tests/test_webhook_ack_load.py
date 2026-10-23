"""The webhook ACK load script sends signed pings the real route stores (#163)."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import importlib.util
import json
import sys
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType, TracebackType
from typing import Self

import httpx
import pytest

from app.bootstrap.reviews_api import get_github_webhook_receipt_uow_factory
from app.main import app, get_github_webhook_secret
from app.modules.integrations.webhooks.application.receive_github_delivery import WebhookReceipt

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


def test_every_delivery_is_a_signed_ping_the_route_stores(
    load: ModuleType, receipts: MemoryReceipts
) -> None:
    report = asyncio.run(
        load.run(
            url=_URL,
            secret=_SECRET,
            count=12,
            concurrency=3,
            body_bytes=4096,
            timeout=5.0,
            transport=_app_transport(),
        )
    )

    # 5 warm-up + 12 timed deliveries, each a fresh receipt.
    assert report.statuses == {"202": 17}
    assert report.count == 12 and report.warmup == 5
    assert len(receipts.rows) == 17 and receipts.commits == 17
    for delivery_id, receipt in receipts.rows.items():
        assert delivery_id.startswith("loadtest-") and len(delivery_id) == 45
        assert receipt.event_name == "ping"
        payload = json.loads(receipt.payload_json)
        assert "installation" not in payload
        assert payload["zen"] == "Load test, ignore."
        # 4096 bytes minus the 53-byte unpadded body.
        assert len(payload["padding"]) == 4043
    assert report.latency_ms is not None
    assert report.target_host == "test"
    assert report.all_accepted


def test_ping_body_has_the_requested_size_and_github_signature(load: ModuleType) -> None:
    body, headers = load.build_ping("s3cret", "loadtest-1", 16384)

    assert len(body) == 16384
    assert json.loads(body)["hook_id"] == 0
    expected = hmac.new(b"s3cret", body, hashlib.sha256).hexdigest()
    assert headers == {
        "Content-Type": "application/json",
        "X-GitHub-Event": "ping",
        "X-GitHub-Delivery": "loadtest-1",
        "X-Hub-Signature-256": f"sha256={expected}",
    }


def test_in_flight_requests_never_exceed_the_concurrency(load: ModuleType) -> None:
    in_flight = 0
    peak = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        await asyncio.sleep(0.01)
        in_flight -= 1
        return httpx.Response(202, json={"status": "pending"})

    report = asyncio.run(
        load.run(
            url=_URL,
            secret=_SECRET,
            count=20,
            concurrency=4,
            body_bytes=1024,
            timeout=5.0,
            transport=httpx.MockTransport(handler),
        )
    )

    assert report.statuses == {"202": 25}
    assert peak == 4


@pytest.mark.parametrize(
    "argv,message",
    [
        (["--count", "1001"], "--count must be from 1 to 1000"),
        (["--count", "0"], "--count must be from 1 to 1000"),
        (["--concurrency", "11"], "--concurrency must be from 1 to 10"),
        (["--body-bytes", "65537"], "--body-bytes must be from 1 to 65536"),
    ],
    ids=["count-over-cap", "count-zero", "concurrency-over-cap", "body-over-cap"],
)
def test_values_over_a_cap_exit_2_before_any_request(
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
    assert len(calls) == 1005
    assert len(calls[-1].content) == 65536


def test_missing_secret_exits_2_without_a_request(
    load: ModuleType, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("GITHUB_WEBHOOK_SECRET", raising=False)
    calls: list[httpx.Request] = []

    assert load.main(["--url", _URL], transport=_recording(calls)) == 2
    assert "GITHUB_WEBHOOK_SECRET is not set" in capsys.readouterr().err
    assert calls == []


def test_wrong_secret_gets_401_and_exits_1_with_the_report(
    load: ModuleType,
    receipts: MemoryReceipts,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", "not-the-server-secret")
    report_path = tmp_path / "report.json"
    argv = ["--url", _URL, "--count", "3", "--report-json", str(report_path)]

    assert load.main(argv, transport=_app_transport()) == 1
    report = json.loads(report_path.read_text())
    assert report["statuses"] == {"401": 8}
    assert report["latency_ms"] is None
    assert report["verdict"] == (
        "no 202 answer among the timed deliveries: no latency to compare with SD §13"
    )
    assert receipts.rows == {}


def test_transport_error_exits_1(load: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", _SECRET)

    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    assert load.main(["--url", _URL, "--count", "2"], transport=httpx.MockTransport(refuse)) == 1


def test_report_json_has_the_run_parameters_and_targets(
    load: ModuleType, receipts: MemoryReceipts, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", _SECRET)
    report_path = tmp_path / "report.json"
    argv = [
        "--url",
        "http://test:8080/webhooks/github",
        "--count",
        "4",
        "--concurrency",
        "2",
        "--body-bytes",
        "2048",
        "--report-json",
        str(report_path),
    ]

    assert load.main(argv, transport=_app_transport()) == 0
    report = json.loads(report_path.read_text())
    assert {key: report[key] for key in ("target_host", "count", "warmup", "concurrency")} == {
        "target_host": "test:8080",
        "count": 4,
        "warmup": 5,
        "concurrency": 2,
    }
    assert report["body_bytes"] == 2048
    assert report["statuses"] == {"202": 9}
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


@pytest.mark.parametrize(
    "p95,expected",
    [
        (
            199.9,
            "p95 199.9 ms meets the SD §13 target (p95 < 500 ms) and the internal goal (200 ms)",
        ),
        (
            200.0,
            "p95 200.0 ms meets the SD §13 target (p95 < 500 ms) "
            "but misses the internal goal (200 ms)",
        ),
        (
            499.9,
            "p95 499.9 ms meets the SD §13 target (p95 < 500 ms) "
            "but misses the internal goal (200 ms)",
        ),
        (500.0, "p95 500.0 ms misses the SD §13 target (p95 < 500 ms)"),
    ],
    ids=["within-goal", "at-goal", "below-target", "at-target"],
)
def test_verdict_compares_p95_with_sd_13(load: ModuleType, p95: float, expected: str) -> None:
    assert load.verdict(p95) == expected


def _recording(calls: list[httpx.Request]) -> httpx.MockTransport:
    def accept(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(202, json={"status": "pending"})

    return httpx.MockTransport(accept)
