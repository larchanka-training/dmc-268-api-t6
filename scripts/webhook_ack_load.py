#!/usr/bin/env python3
"""Concurrent webhook ACK latency load against `POST /webhooks/github` (#163, SD §13).

Sends `--count` signed `ping` deliveries, `--concurrency` at a time, after a warm-up of one
delivery per connection (`--concurrency` of them, in parallel) that is sent and counted but left
out of the latency figures. Every delivery has a fresh `loadtest-<uuid4>` id, its own HMAC-SHA256
signature of the raw body and no `installation` key. The JSON body is padded to exactly
`--body-bytes` with an array of small, varied objects (numbers, strings, booleans, nulls), so the
server's HMAC, JSON and JSONB work resembles a real delivery's. The API stores each one as a
receipt and the webhook worker ends it as `ignored_irrelevant_event`: no Run, no GitHub or LLM
call; the receipt is purged after 30 days.

Usage:
    GITHUB_WEBHOOK_SECRET=... uv run python scripts/webhook_ack_load.py \\
        --url https://staging-api.example.org/webhooks/github \\
        [--count 200] [--concurrency 5] [--body-bytes 16384] [--report-json report.json] \\
        [--environment staging] [--commit SHA] [--window "agreed window, approver, link"]

The caps (count 1000, concurrency 10, body 64 KiB) protect the VPS that staging shares with
production, and so does the fail-fast: the run stops sending when a warm-up delivery does not get
202, after 5 failed timed deliveries in a row, or when more than 10 % of the timed deliveries
failed once at least 20 of them are done; the partial report is still written. Latency is
client-side and nearest-rank, over the 202 answers of the timed deliveries, cut to 0.01 ms; the
verdict compares the uncut p95. The client keeps up to `--concurrency` connections alive. The
secret is read only from GITHUB_WEBHOOK_SECRET and is never printed.

Exit codes: 0 every delivery got 202 (a p95 over the target is reported, not an error);
1 a non-202 answer, a transport error or a fail-fast stop; 2 invalid arguments or no secret.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import hmac
import json
import math
import os
import sys
import time
import uuid
from collections import Counter
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from decimal import ROUND_DOWN, Decimal
from functools import cache
from pathlib import Path
from urllib.parse import urlsplit

import httpx

SECRET_ENV = "GITHUB_WEBHOOK_SECRET"
WEBHOOK_PATH = "/webhooks/github"
DELIVERY_PREFIX = "loadtest-"
DEFAULT_COUNT = 200
MAX_COUNT = 1000
DEFAULT_CONCURRENCY = 5
MAX_CONCURRENCY = 10
DEFAULT_BODY_BYTES = 16384
MAX_BODY_BYTES = 65536
# Fail-fast over the timed deliveries (the warm-up stops the run on any non-202 by itself).
MAX_CONSECUTIVE_FAILURES = 5
MAX_FAILURE_SHARE = 0.10
FAILURE_SHARE_AFTER = 20
# SD §13: webhook ACK p95 < 500 ms, internal goal 200 ms.
TARGET_P95_MS = 500.0
GOAL_P95_MS = 200.0
TRANSPORT_ERROR = "transport_error"
WARMUP_FAILED = "a warm-up delivery did not get 202"


@dataclass(frozen=True)
class Latency:
    p50: float
    p95: float
    p99: float
    max: float


@dataclass(frozen=True)
class LoadReport:
    started_at: str
    finished_at: str
    environment: str | None
    commit: str | None
    window: str | None
    target_host: str
    count: int
    warmup: int
    sent: int
    concurrency: int
    body_bytes: int
    statuses: dict[str, int]
    aborted: str | None
    latency_ms: Latency | None
    verdict: str

    @property
    def failed(self) -> int:
        return sum(value for key, value in self.statuses.items() if key != "202")

    @property
    def all_accepted(self) -> bool:
        """Whether every warm-up and timed delivery was sent and got 202."""
        return self.failed == 0 and self.aborted is None

    def to_json(self) -> dict[str, object]:
        return {
            **asdict(self),
            "targets_ms": {"p95": TARGET_P95_MS, "p95_internal_goal": GOAL_P95_MS},
        }


def sign(secret: str, body: bytes) -> str:
    """Return the `X-Hub-Signature-256` value GitHub sends for this raw body."""
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _compact(value: object) -> bytes:
    return json.dumps(value, separators=(",", ":")).encode()


def padding_item(index: int) -> dict[str, object]:
    """One small object of the padding array; the values vary with ``index``."""
    return {
        "id": index,
        "path": f"src/module_{index % 37}/file_{index}.py",
        "status": ("added", "modified", "removed", "renamed")[index % 4],
        "additions": (index * 7) % 113,
        "deletions": (index * 3) % 29,
        "binary": index % 11 == 0,
        "score": round((index % 17) / 16, 3),
        "sha": None if index % 5 == 0 else f"{index:040x}",
    }


@cache
def ping_body(body_bytes: int) -> bytes:
    """A `ping` body of exactly ``body_bytes`` when that is above its unpadded size.

    Whole padding items fill it as far as they fit; the `fill` string takes the rest.
    """
    items: list[dict[str, object]] = []
    payload: dict[str, object] = {
        "zen": "Load test, ignore.",
        "hook_id": 0,
        "padding": items,
        "fill": "",
    }
    size = len(_compact(payload))
    while True:
        grown = size + len(_compact(padding_item(len(items)))) + (1 if items else 0)
        if grown > body_bytes:
            break
        items.append(padding_item(len(items)))
        size = grown
    payload["fill"] = "x" * max(0, body_bytes - size)
    return _compact(payload)


def build_ping(secret: str, delivery_id: str, body_bytes: int) -> tuple[bytes, dict[str, str]]:
    """Return the raw body and headers of a signed `ping` without `installation`."""
    body = ping_body(body_bytes)
    headers = {
        "Content-Type": "application/json",
        "X-GitHub-Event": "ping",
        "X-GitHub-Delivery": delivery_id,
        "X-Hub-Signature-256": sign(secret, body),
    }
    return body, headers


def percentile(samples: Sequence[float], q: float) -> float:
    """Nearest-rank percentile: the smallest sample with at least q% of samples at or below it."""
    ordered = sorted(samples)
    rank = max(1, math.ceil(q / 100 * len(ordered)))
    return ordered[rank - 1]


def truncated(value: float) -> float:
    """``value`` cut, not rounded, to 0.01 ms: a printed figure under a target is under it."""
    return float(Decimal(repr(value)).quantize(Decimal("0.01"), rounding=ROUND_DOWN))


def verdict(p95_ms: float | None, *, failed: int = 0, sent: int = 0) -> str:
    """The comparison of the unrounded p95 with SD §13; INVALID when a delivery failed."""
    shown = f"{truncated(p95_ms):.2f}" if p95_ms is not None else ""
    if p95_ms is None:
        text = "no 202 answer among the timed deliveries: no latency to compare with SD §13"
    elif p95_ms >= TARGET_P95_MS:
        text = f"p95 {shown} ms misses the SD §13 target (p95 < {TARGET_P95_MS:.0f} ms)"
    elif p95_ms >= GOAL_P95_MS:
        text = (
            f"p95 {shown} ms meets the SD §13 target (p95 < {TARGET_P95_MS:.0f} ms) "
            f"but misses the internal goal ({GOAL_P95_MS:.0f} ms)"
        )
    else:
        text = (
            f"p95 {shown} ms meets the SD §13 target (p95 < {TARGET_P95_MS:.0f} ms) "
            f"and the internal goal ({GOAL_P95_MS:.0f} ms)"
        )
    if failed:
        return f"INVALID ({failed} of {sent} deliveries failed): {text}"
    return text


async def _send(
    client: httpx.AsyncClient, url: str, secret: str, body_bytes: int
) -> tuple[str, float]:
    """POST one fresh delivery; return its status key and elapsed milliseconds."""
    body, headers = build_ping(secret, f"{DELIVERY_PREFIX}{uuid.uuid4()}", body_bytes)
    started = time.perf_counter()
    try:
        response = await client.post(url, content=body, headers=headers)
    except httpx.TransportError:
        return TRANSPORT_ERROR, (time.perf_counter() - started) * 1000
    return str(response.status_code), (time.perf_counter() - started) * 1000


class _FailFast:
    """Counts timed outcomes in completion order and names the reason to stop, if any."""

    def __init__(self) -> None:
        self.done = 0
        self.failed = 0
        self.in_a_row = 0
        self.reason: str | None = None

    def record(self, accepted: bool) -> None:
        self.done += 1
        self.failed += 0 if accepted else 1
        self.in_a_row = 0 if accepted else self.in_a_row + 1
        if self.reason is not None:
            return
        if self.in_a_row >= MAX_CONSECUTIVE_FAILURES:
            self.reason = f"{self.in_a_row} timed deliveries in a row failed"
        elif self.done >= FAILURE_SHARE_AFTER and self.failed > MAX_FAILURE_SHARE * self.done:
            self.reason = f"{self.failed} of {self.done} timed deliveries failed (over 10 %)"


async def run(
    *,
    url: str,
    secret: str,
    count: int,
    concurrency: int,
    body_bytes: int,
    timeout: float,
    environment: str | None = None,
    commit: str | None = None,
    window: str | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> LoadReport:
    """Warm up every connection, then send ``count`` deliveries with ``concurrency`` in flight."""
    statuses: Counter[str] = Counter()
    latencies: list[float] = []
    semaphore = asyncio.Semaphore(concurrency)
    fail_fast = _FailFast()
    aborted: str | None = None
    started_at = datetime.now(UTC)
    async with httpx.AsyncClient(
        transport=transport,
        timeout=timeout,
        limits=httpx.Limits(max_connections=concurrency, max_keepalive_connections=concurrency),
    ) as client:
        warmup = await asyncio.gather(
            *(_send(client, url, secret, body_bytes) for _ in range(concurrency))
        )
        statuses.update(status for status, _elapsed in warmup)
        if any(status != "202" for status, _elapsed in warmup):
            aborted = WARMUP_FAILED
        else:

            async def timed() -> None:
                async with semaphore:
                    # Queued deliveries are dropped once the fail-fast has a reason.
                    if fail_fast.reason is not None:
                        return
                    status, elapsed = await _send(client, url, secret, body_bytes)
                statuses[status] += 1
                fail_fast.record(status == "202")
                if status == "202":
                    latencies.append(elapsed)

            await asyncio.gather(*(timed() for _ in range(count)))
            aborted = fail_fast.reason
    finished_at = datetime.now(UTC)
    latency = (
        Latency(
            p50=truncated(percentile(latencies, 50)),
            p95=truncated(percentile(latencies, 95)),
            p99=truncated(percentile(latencies, 99)),
            max=truncated(max(latencies)),
        )
        if latencies
        else None
    )
    parts = urlsplit(url)
    host = parts.hostname or ""
    sent = sum(statuses.values())
    failed = sent - statuses["202"]
    return LoadReport(
        started_at=started_at.isoformat().replace("+00:00", "Z"),
        finished_at=finished_at.isoformat().replace("+00:00", "Z"),
        environment=environment,
        commit=commit,
        window=window,
        target_host=f"{host}:{parts.port}" if parts.port else host,
        count=count,
        warmup=concurrency,
        sent=sent,
        concurrency=concurrency,
        body_bytes=body_bytes,
        statuses=dict(sorted(statuses.items())),
        aborted=aborted,
        latency_ms=latency,
        verdict=verdict(percentile(latencies, 95) if latencies else None, failed=failed, sent=sent),
    )


def print_report(report: LoadReport) -> None:
    statuses = ", ".join(f"{key}={value}" for key, value in report.statuses.items())
    print(
        f"webhook ACK load ({report.environment or 'no environment label'}, "
        f"commit {report.commit or '-'}): {report.count} timed + {report.warmup} warm-up "
        f"deliveries to {report.target_host} (concurrency {report.concurrency}, "
        f"body {report.body_bytes} bytes)"
    )
    if report.window:
        print(f"window: {report.window}")
    print(f"sent: {report.sent}, statuses: {statuses}")
    if report.aborted:
        print(f"stopped early: {report.aborted}")
    if report.latency_ms is not None:
        latency = report.latency_ms
        print(
            f"latency ms: p50 {latency.p50:.2f}, p95 {latency.p95:.2f}, "
            f"p99 {latency.p99:.2f}, max {latency.max:.2f}"
        )
    print(f"verdict: {report.verdict}")


def main(
    argv: Sequence[str] | None = None, *, transport: httpx.AsyncBaseTransport | None = None
) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument(
        "--url", required=True, help=f"webhook URL, e.g. https://host{WEBHOOK_PATH}"
    )
    parser.add_argument(
        "--count",
        type=int,
        default=DEFAULT_COUNT,
        help=f"timed deliveries (default {DEFAULT_COUNT}, at most {MAX_COUNT})",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=DEFAULT_CONCURRENCY,
        help=f"requests in flight (default {DEFAULT_CONCURRENCY}, at most {MAX_CONCURRENCY})",
    )
    parser.add_argument(
        "--body-bytes",
        type=int,
        default=DEFAULT_BODY_BYTES,
        help=f"JSON body size (default {DEFAULT_BODY_BYTES}, at most {MAX_BODY_BYTES})",
    )
    parser.add_argument("--timeout", type=float, default=10.0, help="seconds per request")
    parser.add_argument("--report-json", type=Path, help="write the report as JSON here")
    parser.add_argument("--environment", help="environment label for the report, e.g. staging")
    parser.add_argument("--commit", help="commit SHA of the code that runs the load")
    parser.add_argument("--window", help="the agreed load window, approver and issue link")
    args = parser.parse_args(argv)
    for flag, value, maximum in (
        ("--count", args.count, MAX_COUNT),
        ("--concurrency", args.concurrency, MAX_CONCURRENCY),
        ("--body-bytes", args.body_bytes, MAX_BODY_BYTES),
    ):
        if not 1 <= value <= maximum:
            parser.error(f"{flag} must be from 1 to {maximum} (the cap protects the staging VPS)")
    if not args.timeout > 0:
        parser.error("--timeout must be positive")
    url = urlsplit(args.url)
    if url.scheme not in {"http", "https"} or not url.hostname:
        parser.error("--url must be an http:// or https:// URL with a host")
    if url.path != WEBHOOK_PATH:
        parser.error(f"--url must point at {WEBHOOK_PATH}")

    secret = os.environ.get(SECRET_ENV, "")
    if not secret:
        print(f"error: {SECRET_ENV} is not set; export the server's secret", file=sys.stderr)
        return 2

    report = asyncio.run(
        run(
            url=args.url,
            secret=secret,
            count=args.count,
            concurrency=args.concurrency,
            body_bytes=args.body_bytes,
            timeout=args.timeout,
            environment=args.environment,
            commit=args.commit,
            window=args.window,
            transport=transport,
        )
    )
    print_report(report)
    if args.report_json is not None:
        args.report_json.write_text(json.dumps(report.to_json(), indent=2) + "\n")
    if not report.all_accepted:
        print("FAIL: a delivery did not get 202 (see statuses)", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
