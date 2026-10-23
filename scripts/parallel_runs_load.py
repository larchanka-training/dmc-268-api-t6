#!/usr/bin/env python3
"""Five parallel Runs on local compose with a fake GitHub and a fake LLM (#163, SD §13).

SD §13 sets v1 throughput at 100 PR/day and 5 parallel Runs, scaled by `worker` replicas. This
driver checks that on a disposable stack:

1. `docker compose -p <project> ... up -d --build --wait --scale worker=<runs>` with
   deploy/load/compose.parallel-runs.yml: the `stub` service (scripts/review_load_stub.py) is
   GitHub and the LLM, every host port is a free one, the App key and webhook secret are
   throwaway values made here;
2. seeds installation 17 / repository 101 (`wait_for_ci = never`, one active rule version) as in
   docs/WEBHOOK_WORKER.md, then sends one signed `pull_request` / `labeled` (`ai-review`) delivery
   per PR 1..<runs> to the backend;
3. polls `runs` until every Run is terminal, then reports per Run its state, attempt, worker,
   projection wait (`created_at - webhook_events.received_at`), queue wait
   (`started_at - created_at`), broker time (`started_at - message_published_at`) and total time
   (`finished_at - created_at`); the distinct workers, the peak of Runs in flight, review POSTs per
   PR from the stub and the depth of `reviews.dlq`;
4. `down -v` of its own project, unless `--keep`.

Usage:
    uv run python scripts/parallel_runs_load.py --project dmc163load \\
        [--runs 5] [--llm-delay-ms 3000] [--keep] [--report-json report.json]

The verdict passes when every Run succeeded on attempt 1, each PR has exactly one Run and one
posted review, and the DLQ is empty. The project name must differ from the shared local stand
`dmc-268-api-t6`: `down -v` deletes the project's volumes.

Exit codes: 0 the verdict passed; 1 it failed, or the stack did not start or finish in time;
2 invalid arguments.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import time
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path

import httpx
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat

REPO_ROOT = Path(__file__).resolve().parents[1]
OVERRIDE = "deploy/load/compose.parallel-runs.yml"
SHARED_PROJECT = "dmc-268-api-t6"
_PROJECT_NAME = re.compile(r"[a-z0-9][a-z0-9_-]*")
MAX_RUNS = 20
TERMINAL_STATES = frozenset({"succeeded", "failed", "cancelled", "skipped"})
DLQ = "reviews.dlq"
# The same PRs as scripts/review_load_stub.py (tests/test_parallel_runs_load.py keeps them equal).
INSTALLATION_ID = 17
REPOSITORY_ID = 101
REPOSITORY = "load/parallel-runs"
PR_ID_OFFSET = 9000
BASE_SHA = "b" * 40
# Host ports: flag -> compose variable. The base file's defaults are the shared stand's ports.
PORT_FLAGS = {
    "backend_port": "BACKEND_PORT",
    "postgres_port": "POSTGRES_PORT",
    "rabbitmq_port": "RABBITMQ_PORT",
    "rabbitmq_management_port": "RABBITMQ_MANAGEMENT_PORT",
    "redis_port": "REDIS_PORT",
    "stub_port": "LOAD_STUB_PORT",
}
# Pinned so a developer's shell cannot point psql at another user or database.
DATABASE_ENV = {
    "POSTGRES_USER": "app",
    "POSTGRES_PASSWORD": "app",
    "POSTGRES_DB": "app",
    "RABBITMQ_USER": "app",
    "RABBITMQ_PASSWORD": "app",
}

SEED_SQL = f"""
INSERT INTO workspaces (id, name, daily_budget_usd) VALUES (gen_random_uuid(), 'Load', 0);
INSERT INTO provider_installations (id, workspace_id, provider, external_id, metadata)
  SELECT gen_random_uuid(), id, 'github', {INSTALLATION_ID}, '{{}}'::jsonb
  FROM workspaces WHERE name = 'Load';
INSERT INTO repositories (id, provider_installation_id, external_id, full_name,
                          default_branch, web_url, wait_for_ci)
  SELECT gen_random_uuid(), id, {REPOSITORY_ID}, '{REPOSITORY}', 'main',
         'https://github.com/{REPOSITORY}', 'never'
  FROM provider_installations WHERE external_id = {INSTALLATION_ID};
INSERT INTO rule_versions (id, repository_id, version, rules, checksum, is_active)
  SELECT gen_random_uuid(), id, 1, '[]'::jsonb, repeat('a', 64), true
  FROM repositories WHERE external_id = {REPOSITORY_ID};
"""

# Epoch seconds, so no timestamp text is parsed; one JSON array per poll.
RUNS_SQL = """
SELECT coalesce(json_agg(row_to_json(r) ORDER BY r.pr, r.created), '[]'::json) FROM (
  SELECT cc.external_number AS pr, r.state::text AS state, r.attempt, r.worker_id,
         r.error_code,
         extract(epoch FROM w.received_at)::float8 AS received,
         extract(epoch FROM r.created_at)::float8 AS created,
         extract(epoch FROM r.message_published_at)::float8 AS published,
         extract(epoch FROM r.started_at)::float8 AS started,
         extract(epoch FROM r.finished_at)::float8 AS finished
  FROM runs r
  JOIN code_changes cc ON cc.id = r.code_change_id
  LEFT JOIN LATERAL (
    SELECT min(e.received_at) AS received_at FROM webhook_events e
    WHERE e.event = 'pull_request'
      AND e.payload -> 'pull_request' ->> 'number' = cc.external_number::text
  ) w ON true
) r
"""


def project_error(project: str | None) -> str | None:
    """Why ``project`` may not be used, or None when it is a separate compose project."""
    if not project:
        return "--project is required: the load stack runs under its own compose project"
    if not _PROJECT_NAME.fullmatch(project):
        return "--project must be lowercase letters, digits, '-' or '_'"
    if project == SHARED_PROJECT:
        return f"--project must differ from the shared local stand {SHARED_PROJECT!r}"
    return None


def compose_command(project: str, *args: str) -> list[str]:
    """`docker compose` for the load project; the empty --env-file keeps any `.env` out."""
    return [
        "docker",
        "compose",
        "-p",
        project,
        "--env-file",
        os.devnull,
        "-f",
        "docker-compose.yml",
        "-f",
        OVERRIDE,
        "--profile",
        "webhooks",
        *args,
    ]


def head_sha(number: int) -> str:
    """The head of PR ``number``, as the stub answers it."""
    return f"{number:02x}" * 20


def build_delivery(secret: str, number: int) -> tuple[bytes, dict[str, str]]:
    """A signed `pull_request` / `labeled` (`ai-review`) delivery for PR ``number``."""
    payload = {
        "action": "labeled",
        "installation": {"id": INSTALLATION_ID},
        "repository": {"id": REPOSITORY_ID, "full_name": REPOSITORY},
        "label": {"name": "ai-review"},
        "pull_request": {
            "id": PR_ID_OFFSET + number,
            "number": number,
            "title": f"Load change {number}",
            "html_url": f"https://github.com/{REPOSITORY}/pull/{number}",
            "user": {"login": "load-author"},
            "head": {"ref": f"load-{number}", "sha": head_sha(number)},
            "base": {"ref": "main", "sha": BASE_SHA},
            "state": "open",
            "updated_at": "2026-10-23T00:00:00Z",
        },
    }
    body = json.dumps(payload).encode()
    signature = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    headers = {
        "Content-Type": "application/json",
        "X-GitHub-Event": "pull_request",
        "X-GitHub-Delivery": f"loadtest-runs-{uuid.uuid4()}",
        "X-Hub-Signature-256": f"sha256={signature}",
    }
    return body, headers


@dataclass(frozen=True)
class RunMetrics:
    pr: int
    state: str
    attempt: int
    worker_id: str | None
    error_code: str | None
    projection_ms: float | None
    queue_wait_ms: float | None
    broker_ms: float | None
    total_ms: float | None


def _span_ms(end: object, start: object) -> float | None:
    if not isinstance(end, int | float) or not isinstance(start, int | float):
        return None
    return round((end - start) * 1000, 1)


def run_metrics(rows: Sequence[Mapping[str, object]]) -> list[RunMetrics]:
    """Per-Run timings from the rows of RUNS_SQL (epoch seconds)."""
    return [
        RunMetrics(
            pr=int(str(row["pr"])),
            state=str(row["state"]),
            attempt=int(str(row["attempt"])),
            worker_id=None if row["worker_id"] is None else str(row["worker_id"]),
            error_code=None if row["error_code"] is None else str(row["error_code"]),
            projection_ms=_span_ms(row["created"], row["received"]),
            queue_wait_ms=_span_ms(row["started"], row["created"]),
            broker_ms=_span_ms(row["started"], row["published"]),
            total_ms=_span_ms(row["finished"], row["created"]),
        )
        for row in rows
    ]


def max_parallel(rows: Sequence[Mapping[str, object]]) -> int:
    """The most Runs between `started_at` and `finished_at` at the same moment."""
    events: list[tuple[float, int]] = []
    for row in rows:
        started, finished = row["started"], row["finished"]
        if isinstance(started, int | float) and isinstance(finished, int | float):
            # At equal times the finish (-1) sorts first: back-to-back Runs do not overlap.
            events += [(float(started), 1), (float(finished), -1)]
    peak = current = 0
    for _, delta in sorted(events):
        current += delta
        peak = max(peak, current)
    return peak


def verdict(
    metrics: Sequence[RunMetrics],
    *,
    runs: int,
    review_posts: Mapping[str, int],
    dlq_depth: int | None,
) -> dict[str, bool]:
    """Each rule of the scenario; it passes only when all are true."""
    return {
        "all_succeeded": bool(metrics) and all(m.state == "succeeded" for m in metrics),
        "all_first_attempt": bool(metrics) and all(m.attempt == 1 for m in metrics),
        "one_run_per_pr": sorted(m.pr for m in metrics) == list(range(1, runs + 1)),
        "one_review_per_pr": dict(review_posts) == {str(n): 1 for n in range(1, runs + 1)},
        "dlq_empty": dlq_depth == 0,
    }


def throwaway_app_key() -> str:
    """A fresh RSA key for the App JWT; the stub never checks the signature."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()).decode()


def free_ports(count: int) -> list[int]:
    """Distinct ports nothing listens on right now (a later bind can still race)."""
    ports: set[int] = set()
    while len(ports) < count:
        with socket.socket() as sock:
            sock.bind(("", 0))
            ports.add(int(sock.getsockname()[1]))
    return sorted(ports)


class Stack:
    """The compose project of one load run; every command carries the same project and env."""

    def __init__(self, project: str, env: Mapping[str, str]) -> None:
        self.project = project
        self.env = dict(env)

    def run(
        self, *args: str, stdin: str | None = None, capture: bool = True, check: bool = True
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            compose_command(self.project, *args),
            cwd=REPO_ROOT,
            env=self.env,
            input=stdin,
            stdout=subprocess.PIPE if capture else sys.stderr,
            stderr=subprocess.PIPE if capture else None,
            text=True,
            check=check,
        )

    def psql(self, sql: str, *, query: bool = False) -> str:
        flags = ["-tA"] if query else []
        args = ["exec", "-T", "postgres", "psql", "-U", "app", "-d", "app", "-v", "ON_ERROR_STOP=1"]
        return self.run(*args, *flags, stdin=sql).stdout

    def rows(self) -> list[dict[str, object]]:
        decoded = json.loads(self.psql(RUNS_SQL, query=True))
        assert isinstance(decoded, list)
        return decoded

    def dlq_depth(self) -> int | None:
        listed = self.run(
            "exec",
            "-T",
            "rabbitmq",
            "rabbitmqctl",
            "list_queues",
            "--quiet",
            "--formatter",
            "json",
            "name",
            "messages",
        ).stdout
        for queue in json.loads(listed):
            if queue.get("name") == DLQ:
                return int(queue["messages"])
        return None

    def log_tail(self) -> None:
        tail = self.run(
            "logs", "--no-color", "--tail", "40", "worker", "webhook-worker", "stub", check=False
        )
        sys.stderr.write(tail.stdout + tail.stderr)


def _fail(stack: Stack, message: str) -> int:
    print(f"FAIL: {message}", file=sys.stderr)
    stack.log_tail()
    return 1


def scenario(stack: Stack, args: argparse.Namespace, secret: str) -> int:
    ports = {name: int(stack.env[name]) for name in PORT_FLAGS.values()}
    began = time.monotonic()
    try:
        stack.run("up", "-d", "--build", "--wait", "--scale", f"worker={args.runs}", capture=False)
    except subprocess.CalledProcessError:
        return _fail(stack, "the load stack did not start (docker compose up)")
    stack_seconds = round(time.monotonic() - began, 1)
    stack.psql(SEED_SQL)

    sent_at = time.monotonic()
    backend = f"http://127.0.0.1:{ports['BACKEND_PORT']}/webhooks/github"
    with httpx.Client(timeout=10) as client:
        for number in range(1, args.runs + 1):
            body, headers = build_delivery(secret, number)
            response = client.post(backend, content=body, headers=headers)
            if response.status_code != 202 or response.json() != {"status": "pending"}:
                return _fail(stack, f"PR {number}: {response.status_code} {response.text[:200]}")

    rows: list[dict[str, object]] = []
    deadline = sent_at + args.timeout
    while time.monotonic() < deadline:
        rows = stack.rows()
        if len(rows) >= args.runs and all(row["state"] in TERMINAL_STATES for row in rows):
            break
        time.sleep(1)
    else:
        states = [(row["pr"], row["state"]) for row in rows]
        return _fail(stack, f"Runs not terminal after {args.timeout:.0f} s: {states}")
    runs_seconds = round(time.monotonic() - sent_at, 1)
    # A late duplicate Run would show up here.
    time.sleep(args.settle)
    rows = stack.rows()

    stats = httpx.get(f"http://127.0.0.1:{ports['LOAD_STUB_PORT']}/_stats", timeout=10).json()
    metrics = run_metrics(rows)
    dlq_depth = stack.dlq_depth()
    checks = verdict(
        metrics, runs=args.runs, review_posts=stats["review_posts"], dlq_depth=dlq_depth
    )
    report: dict[str, object] = {
        "project": stack.project,
        "runs": args.runs,
        "llm_delay_ms": args.llm_delay_ms,
        "stack_up_s": stack_seconds,
        "deliveries_to_terminal_s": runs_seconds,
        "per_run": [asdict(m) for m in metrics],
        "distinct_worker_ids": len({m.worker_id for m in metrics if m.worker_id}),
        "max_parallel": max_parallel(rows),
        "review_posts": stats["review_posts"],
        "llm_calls": stats["llm_calls"],
        "unhandled_stub_requests": stats["unhandled"],
        "dlq_depth": dlq_depth,
        "checks": checks,
        "passed": all(checks.values()),
    }
    print_report(report, metrics)
    sys.stdout.flush()  # before the teardown output on stderr
    if args.report_json is not None:
        args.report_json.write_text(json.dumps(report, indent=2) + "\n")
    if not report["passed"]:
        return _fail(
            stack, "the verdict failed: " + ", ".join(k for k, v in checks.items() if not v)
        )
    return 0


def print_report(report: Mapping[str, object], metrics: Sequence[RunMetrics]) -> None:
    def ms(value: float | None) -> str:
        return "-" if value is None else f"{value:.0f}"

    print(
        f"parallel Runs: {report['runs']} PRs, LLM delay {report['llm_delay_ms']} ms, "
        f"stack up in {report['stack_up_s']} s, deliveries to terminal in "
        f"{report['deliveries_to_terminal_s']} s"
    )
    print("pr  state      attempt  projection_ms  queue_wait_ms  broker_ms  total_ms  worker_id")
    for m in metrics:
        print(
            f"{m.pr:<3} {m.state:<10} {m.attempt:<8} {ms(m.projection_ms):>13}  "
            f"{ms(m.queue_wait_ms):>13}  {ms(m.broker_ms):>9}  {ms(m.total_ms):>8}  {m.worker_id}"
        )
    print(
        f"distinct workers: {report['distinct_worker_ids']}, max parallel: "
        f"{report['max_parallel']}, review POSTs: {report['review_posts']}, "
        f"LLM calls: {report['llm_calls']}, DLQ depth: {report['dlq_depth']}"
    )
    if report["unhandled_stub_requests"]:
        print(f"unhandled stub requests: {report['unhandled_stub_requests']}")
    checks = report["checks"]
    assert isinstance(checks, dict)
    print("verdict: " + ("PASS" if report["passed"] else "FAIL"))
    for name, passed in checks.items():
        print(f"  {'ok ' if passed else 'FAIL'} {name}")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--project", help="compose project name (not dmc-268-api-t6)")
    parser.add_argument("--runs", type=int, default=5, help=f"PRs and workers (1-{MAX_RUNS})")
    parser.add_argument("--llm-delay-ms", type=int, default=3000, help="delay of each LLM answer")
    parser.add_argument("--timeout", type=float, default=300.0, help="seconds to terminal Runs")
    parser.add_argument("--settle", type=float, default=5.0, help="seconds before the recount")
    parser.add_argument("--keep", action="store_true", help="leave the stack up (no down -v)")
    parser.add_argument("--report-json", type=Path, help="write the report as JSON here")
    for flag, variable in PORT_FLAGS.items():
        parser.add_argument(f"--{flag.replace('_', '-')}", type=int, help=f"host {variable}")
    args = parser.parse_args(argv)
    error = project_error(args.project)
    if error is not None:
        print(f"error: {error}", file=sys.stderr)
        return 2
    if not 1 <= args.runs <= MAX_RUNS:
        parser.error(f"--runs must be from 1 to {MAX_RUNS}")
    if args.llm_delay_ms < 0 or args.timeout <= 0 or args.settle < 0:
        parser.error("--llm-delay-ms and --settle must be >= 0, --timeout > 0")
    if shutil.which("docker") is None:
        print("error: docker is not on PATH", file=sys.stderr)
        return 2

    fixed = {variable: getattr(args, flag) for flag, variable in PORT_FLAGS.items()}
    free = iter(free_ports(len(fixed)))
    ports = {variable: str(port or next(free)) for variable, port in fixed.items()}
    secret = secrets.token_hex(32)
    env = {
        **os.environ,
        **DATABASE_ENV,
        **ports,
        "LOAD_RUNS": str(args.runs),
        "LOAD_LLM_DELAY_MS": str(args.llm_delay_ms),
        "LOAD_WEBHOOK_SECRET": secret,
        "LOAD_GITHUB_APP_PRIVATE_KEY": throwaway_app_key(),
    }
    stack = Stack(args.project, env)
    print(f"load stack {args.project!r} on host ports {ports}", file=sys.stderr)
    try:
        return scenario(stack, args, secret)
    except subprocess.CalledProcessError as failed:
        command = " ".join(failed.cmd[len(compose_command(stack.project)) :])
        detail = (failed.stderr or "").strip()[-2000:]
        return _fail(stack, f"docker compose {command} exited {failed.returncode}: {detail}")
    except httpx.HTTPError as failed:
        return _fail(stack, f"HTTP request to the load stack failed: {failed!r}")
    finally:
        if args.keep:
            print(f"--keep: the stack {args.project!r} is still up", file=sys.stderr)
        else:
            stack.run("down", "-v", capture=False, check=False)


if __name__ == "__main__":
    raise SystemExit(main())
