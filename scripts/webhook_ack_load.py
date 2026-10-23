#!/usr/bin/env python3
"""Concurrent webhook ACK latency load against `POST /webhooks/github` (#163, SD §13).

Sends `--count` signed `ping` deliveries, `--concurrency` at a time, after a short sequential
warm-up of WARMUP_COUNT deliveries that are sent and counted but left out of the latency figures.
Every delivery has a fresh `loadtest-<uuid4>` id, a fresh HMAC-SHA256 signature of its raw body
and no `installation` key; its JSON body is padded to `--body-bytes` with a `padding` field, so
the server's HMAC, JSON and JSONB work matches a realistic `pull_request` delivery. The API stores
each one as a receipt and the webhook worker ends it as `ignored_irrelevant_event`: no Run, no
GitHub or LLM call; the receipt is purged after 30 days.

Usage:
    GITHUB_WEBHOOK_SECRET=... uv run python scripts/webhook_ack_load.py \\
        --url https://staging-api.example.org/webhooks/github \\
        [--count 200] [--concurrency 5] [--body-bytes 16384] [--report-json report.json]

The caps (count 1000, concurrency 10, body 64 KiB) protect the VPS that staging shares with
production. Latency is client-side and nearest-rank, over the 202 answers of the timed
deliveries; the client keeps up to `--concurrency` connections alive. The secret is read only
from GITHUB_WEBHOOK_SECRET and is never printed.

Exit codes: 0 every delivery got 202 (a p95 over the target is reported, not an error);
1 a non-202 answer or a transport error; 2 invalid arguments or no secret.
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
from pathlib import Path
from urllib.parse import urlsplit

import httpx

SECRET_ENV = "GITHUB_WEBHOOK_SECRET"
DELIVERY_PREFIX = "loadtest-"
DEFAULT_COUNT = 200
MAX_COUNT = 1000
DEFAULT_CONCURRENCY = 5
MAX_CONCURRENCY = 10
DEFAULT_BODY_BYTES = 16384
MAX_BODY_BYTES = 65536
WARMUP_COUNT = 5
# SD §13: webhook ACK p95 < 500 ms, internal goal 200 ms.
TARGET_P95_MS = 500.0
GOAL_P95_MS = 200.0
TRANSPORT_ERROR = "transport_error"


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
    target_host: str
    count: int
    warmup: int
    concurrency: int
    body_bytes: int
    statuses: dict[str, int]
    latency_ms: Latency | None
    verdict: str

    @property
    def all_accepted(self) -> bool:
        """Whether every warm-up and timed delivery got 202."""
        return set(self.statuses) == {"202"}

    def to_json(self) -> dict[str, object]:
        return {
            **asdict(self),
            "targets_ms": {"p95": TARGET_P95_MS, "p95_internal_goal": GOAL_P95_MS},
        }


def sign(secret: str, body: bytes) -> str:
    """Return the `X-Hub-Signature-256` value GitHub sends for this raw body."""
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def build_ping(secret: str, delivery_id: str, body_bytes: int) -> tuple[bytes, dict[str, str]]:
    """Return the raw body and headers of a signed `ping` without `installation`.

    The body is exactly ``body_bytes`` long when that is above the unpadded size.
    """
    payload = {"zen": "Load test, ignore.", "hook_id": 0, "padding": ""}
    unpadded = len(json.dumps(payload, separators=(",", ":")).encode())
    payload["padding"] = "x" * max(0, body_bytes - unpadded)
    body = json.dumps(payload, separators=(",", ":")).encode()
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


def verdict(p95_ms: float | None) -> str:
    """The comparison of the measured p95 with SD §13."""
    if p95_ms is None:
        return "no 202 answer among the timed deliveries: no latency to compare with SD §13"
    head = f"p95 {p95_ms:.1f} ms"
    if p95_ms >= TARGET_P95_MS:
        return f"{head} misses the SD §13 target (p95 < {TARGET_P95_MS:.0f} ms)"
    if p95_ms >= GOAL_P95_MS:
        return (
            f"{head} meets the SD §13 target (p95 < {TARGET_P95_MS:.0f} ms) "
            f"but misses the internal goal ({GOAL_P95_MS:.0f} ms)"
        )
    return (
        f"{head} meets the SD §13 target (p95 < {TARGET_P95_MS:.0f} ms) "
        f"and the internal goal ({GOAL_P95_MS:.0f} ms)"
    )


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


async def run(
    *,
    url: str,
    secret: str,
    count: int,
    concurrency: int,
    body_bytes: int,
    timeout: float,
    transport: httpx.AsyncBaseTransport | None = None,
) -> LoadReport:
    """Send the warm-up, then ``count`` deliveries with at most ``concurrency`` in flight."""
    statuses: Counter[str] = Counter()
    latencies: list[float] = []
    semaphore = asyncio.Semaphore(concurrency)
    started_at = datetime.now(UTC)
    async with httpx.AsyncClient(
        transport=transport,
        timeout=timeout,
        limits=httpx.Limits(max_connections=concurrency, max_keepalive_connections=concurrency),
    ) as client:
        for _ in range(WARMUP_COUNT):
            status, _elapsed = await _send(client, url, secret, body_bytes)
            statuses[status] += 1

        async def timed() -> None:
            async with semaphore:
                status, elapsed = await _send(client, url, secret, body_bytes)
            statuses[status] += 1
            if status == "202":
                latencies.append(elapsed)

        await asyncio.gather(*(timed() for _ in range(count)))
    finished_at = datetime.now(UTC)
    latency = (
        Latency(
            p50=round(percentile(latencies, 50), 1),
            p95=round(percentile(latencies, 95), 1),
            p99=round(percentile(latencies, 99), 1),
            max=round(max(latencies), 1),
        )
        if latencies
        else None
    )
    parts = urlsplit(url)
    host = parts.hostname or ""
    return LoadReport(
        started_at=started_at.isoformat().replace("+00:00", "Z"),
        finished_at=finished_at.isoformat().replace("+00:00", "Z"),
        target_host=f"{host}:{parts.port}" if parts.port else host,
        count=count,
        warmup=WARMUP_COUNT,
        concurrency=concurrency,
        body_bytes=body_bytes,
        statuses=dict(sorted(statuses.items())),
        latency_ms=latency,
        verdict=verdict(latency.p95 if latency else None),
    )


def print_report(report: LoadReport) -> None:
    statuses = ", ".join(f"{key}={value}" for key, value in report.statuses.items())
    print(
        f"webhook ACK load: {report.count} timed + {report.warmup} warm-up deliveries to "
        f"{report.target_host} (concurrency {report.concurrency}, body {report.body_bytes} bytes)"
    )
    print(f"statuses: {statuses}")
    if report.latency_ms is not None:
        latency = report.latency_ms
        print(
            f"latency ms: p50 {latency.p50:.1f}, p95 {latency.p95:.1f}, "
            f"p99 {latency.p99:.1f}, max {latency.max:.1f}"
        )
    print(f"verdict: {report.verdict}")


def main(
    argv: Sequence[str] | None = None, *, transport: httpx.AsyncBaseTransport | None = None
) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument(
        "--url", required=True, help="webhook URL, e.g. https://host/webhooks/github"
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
