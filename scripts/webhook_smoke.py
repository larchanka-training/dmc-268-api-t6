#!/usr/bin/env python3
"""Signed GitHub webhook smoke against a running API (#56).

Sends `pull_request` / `labeled` (`ai-review`) deliveries signed with the HMAC-SHA256 of the raw
body to `POST /webhooks/github` and checks, one line per check:

1. a fresh delivery            -> 202 {"status": "pending"}
2. the same delivery again     -> 202 {"status": "duplicate"}
3. a fresh, corrupted signature -> 401 {"detail": "invalid GitHub webhook signature"}
4. with --count N: N fresh deliveries -> 202 pending each, then p50/p95/max latency in ms
   (client-side, nearest-rank, a new connection per request).

Usage:
    local: uv run --env-file .env python scripts/webhook_smoke.py [--url URL] [--count N]
    CI:    uv run --locked python scripts/webhook_smoke.py \\
               --url http://127.0.0.1:8000/webhooks/github --count 50

The secret is read only from the environment variable GITHUB_WEBHOOK_SECRET (the same value the
server has); the script never prints it or a signature. Every accepted delivery is stored in
`webhook_events`: point it at a disposable stack, never at staging or production.

Exit codes: 0 every check passed; 1 a response did not match; 2 invalid arguments, no secret, or
no valid HTTP answer (the API unreachable or something else listening at --url).
Stdlib only; run through uv on Python 3.13 like every other tool here (AGENTS.md).
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import http.client
import json
import math
import os
import sys
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import Callable, Mapping, Sequence
from urllib.parse import urlsplit

DEFAULT_URL = "http://localhost:8000/webhooks/github"
SECRET_ENV = "GITHUB_WEBHOOK_SECRET"
_BODY_PREVIEW = 200
_HINTS = {
    401: "the script's GITHUB_WEBHOOK_SECRET differs from the server's",
    500: (
        "are the migrations applied (Compose: bootstrap applies them on up; "
        "without Compose: uv run alembic upgrade head)? see the server log"
    ),
    503: "the server has no DATABASE_URL or an empty GITHUB_WEBHOOK_SECRET",
}

type Reply = tuple[int, object, float]
type Expected = tuple[int, object]
type Sender = Callable[[str, bytes, Mapping[str, str], float], Reply]


def sign(secret: str, body: bytes) -> str:
    """Return the `X-Hub-Signature-256` value GitHub sends for this raw body."""
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def build_delivery(secret: str, delivery_id: str) -> tuple[bytes, dict[str, str]]:
    """Return the raw body and headers of a signed `pull_request` / `labeled` delivery."""
    payload = {
        "action": "labeled",
        "installation": {"id": 17},
        "repository": {"id": 101, "full_name": "smoke/webhook-smoke"},
        "label": {"name": "ai-review"},
        "pull_request": {
            "id": 901,
            "number": 7,
            "title": "Webhook smoke",
            "html_url": "https://github.com/smoke/webhook-smoke/pull/7",
            "user": {"login": "webhook-smoke"},
            "head": {"ref": "feature", "sha": "a" * 40},
            "base": {"ref": "main", "sha": "b" * 40},
            "state": "open",
            "updated_at": "2026-10-04T00:00:00Z",
        },
    }
    body = json.dumps(payload).encode()
    headers = {
        "Content-Type": "application/json",
        "X-GitHub-Event": "pull_request",
        "X-GitHub-Delivery": delivery_id,
        "X-Hub-Signature-256": sign(secret, body),
    }
    return body, headers


def corrupt_signature(headers: Mapping[str, str]) -> dict[str, str]:
    """Flip the last hex digit: the value keeps the valid format, so only the HMAC check fails."""
    signature = headers["X-Hub-Signature-256"]
    flipped = "0" if signature[-1] != "0" else "1"
    return {**headers, "X-Hub-Signature-256": signature[:-1] + flipped}


def post_delivery(url: str, body: bytes, headers: Mapping[str, str], timeout: float) -> Reply:
    """POST the delivery; return (status, JSON or text body, elapsed seconds) for any HTTP answer.

    Raises OSError (URLError, TimeoutError, ConnectionError, ...) when no answer arrives and
    http.client.HTTPException (BadStatusLine, ...) when the answer is not valid HTTP.
    """
    request = urllib.request.Request(url, data=body, headers=dict(headers), method="POST")
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status, raw = response.status, response.read()
    except urllib.error.HTTPError as error:
        status, raw = error.code, error.read()
    elapsed = time.perf_counter() - started
    text = raw.decode(errors="replace")
    try:
        return status, json.loads(text), elapsed
    except ValueError:
        return status, text, elapsed


def percentile(samples: Sequence[float], q: float) -> float:
    """Nearest-rank percentile: the smallest sample with at least q% of samples at or below it."""
    ordered = sorted(samples)
    rank = max(1, math.ceil(q / 100 * len(ordered)))
    return ordered[rank - 1]


def _check(
    send: Sender,
    url: str,
    timeout: float,
    label: str,
    delivery: tuple[bytes, Mapping[str, str]],
    expected: Expected,
    *,
    quiet: bool = False,
) -> float | None:
    """Send one delivery; return its latency, or None after printing expected vs got."""
    body, headers = delivery
    status, payload, elapsed = send(url, body, headers, timeout)
    expected_status, expected_payload = expected
    if (status, payload) != (expected_status, expected_payload):
        got = payload if isinstance(payload, str) else json.dumps(payload)
        if len(got) > _BODY_PREVIEW:
            got = got[:_BODY_PREVIEW] + "..."
        print(f"FAIL {label}: expected {expected_status} {json.dumps(expected_payload)}")
        print(f"     got {status} {got}")
        if status in _HINTS:
            print(f"     hint: {_HINTS[status]}")
        return None
    if not quiet:
        print(f"ok   {label}: {status} {json.dumps(payload)} in {elapsed * 1000:.1f} ms")
    return elapsed


def main(argv: Sequence[str] | None = None, *, send: Sender = post_delivery) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--url", default=DEFAULT_URL, help=f"webhook URL (default {DEFAULT_URL})")
    parser.add_argument(
        "--count", type=int, default=0, help="extra fresh deliveries to time (default 0)"
    )
    parser.add_argument("--timeout", type=float, default=10.0, help="seconds per request")
    args = parser.parse_args(argv)
    if args.count < 0:
        parser.error("--count must be >= 0")
    url = urlsplit(args.url)
    if url.scheme not in {"http", "https"} or not url.netloc:
        parser.error("--url must be an http:// or https:// URL with a host")

    secret = os.environ.get(SECRET_ENV, "")
    if not secret:
        print(
            f"error: {SECRET_ENV} is not set; export the server's secret "
            "(locally: uv run --env-file .env python scripts/webhook_smoke.py)",
            file=sys.stderr,
        )
        return 2

    pending: Expected = (202, {"status": "pending"})
    first = build_delivery(secret, str(uuid.uuid4()))
    checks: list[tuple[str, tuple[bytes, Mapping[str, str]], Expected]] = [
        ("fresh delivery", first, pending),
        ("same delivery again", first, (202, {"status": "duplicate"})),
    ]
    body, headers = build_delivery(secret, str(uuid.uuid4()))
    checks.append(
        (
            "corrupted signature",
            (body, corrupt_signature(headers)),
            (401, {"detail": "invalid GitHub webhook signature"}),
        )
    )
    try:
        for label, delivery, expected in checks:
            if _check(send, args.url, args.timeout, label, delivery, expected) is None:
                return 1
        latencies: list[float] = []
        for index in range(1, args.count + 1):
            delivery = build_delivery(secret, str(uuid.uuid4()))
            label = f"fresh delivery {index}/{args.count}"
            elapsed = _check(send, args.url, args.timeout, label, delivery, pending, quiet=True)
            if elapsed is None:
                return 1
            latencies.append(elapsed * 1000)
    except (OSError, http.client.HTTPException) as error:
        print(
            f"error: no HTTP answer from {args.url}: {error}\n"
            "hint: does --url point at the API, and is the stack up "
            "(docker compose up -d --wait backend postgres; bootstrap applies the migrations) "
            "or, without Compose, migrated (uv run alembic upgrade head)?",
            file=sys.stderr,
        )
        return 2
    if latencies:
        print(
            f"ok   {args.count} fresh deliveries: 202 pending each; latency "
            f"p50 {percentile(latencies, 50):.1f} ms, p95 {percentile(latencies, 95):.1f} ms, "
            f"max {max(latencies):.1f} ms"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
