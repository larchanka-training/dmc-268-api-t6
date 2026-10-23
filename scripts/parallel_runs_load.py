#!/usr/bin/env python3
"""Five parallel Runs on local compose with a fake GitHub and a fake LLM (#163, SD §13).

SD §13 sets v1 throughput at 100 PR/day and 5 parallel Runs, scaled by `worker` replicas. This
driver checks that on a disposable stack:

1. refuses a compose project that already has containers or volumes, the shared local stand
   `dmc-268-api-t6` and this checkout's default project name, then runs
   `docker compose -p <project> ... up -d --build --wait --scale worker=<workers>` with
   deploy/load/compose.parallel-runs.yml: the `stub` service (scripts/review_load_stub.py) is
   GitHub and the LLM, every published port is a free loopback port, the App key and webhook
   secret are throwaway values made here, and compose sees only PATH, HOME, DOCKER_* and those;
2. seeds installation 17 / repository 101 (`wait_for_ci = never`, one active rule version) as in
   docs/WEBHOOK_WORKER.md, then sends one signed `pull_request` / `labeled` (`ai-review`) delivery
   per PR 1..<runs> to the backend, and right after them one duplicate per PR (same payload, new
   delivery id);
3. polls until every Run is terminal and every delivery has finished its projection, then
   reports per Run its state, attempt, worker, projection wait
   (`created_at - webhook_events.received_at` of the first delivery), queue wait
   (`started_at - created_at`), broker time (`started_at - message_published_at`) and total time
   (`finished_at - created_at`); the distinct workers, the peak of Runs in flight, review POSTs per
   PR and LLM calls from the stub, the receipt end states and the depth of `reviews.dlq`. A run
   that does not finish in `--timeout` still writes its report;
4. `down -v` of the project it created, unless `--keep` (also after SIGTERM or Ctrl-C).

Usage:
    uv run python scripts/parallel_runs_load.py --project dmc163load \\
        [--runs 5] [--workers N] [--llm-delay-ms 3000] [--max-queue-wait-ms 30000] \\
        [--keep] [--report-json report.json]

The verdict passes when the run finished in time, every Run succeeded on attempt 1, each PR has
exactly one Run, one posted review and one review LLM call despite its duplicate delivery, every
delivery was projected, all `--workers` workers took a Run, at least min(runs, workers) Runs were in
flight at once, no queue wait exceeds `--max-queue-wait-ms` and the DLQ is empty. `--workers` below
`--runs` measures queueing (e.g. 10 PRs on 5 workers). With `--llm-delay-ms` below about 1000 a Run
can finish before the next one starts, so the parallel check may fail by design.

The first `--build` needs registry access (base images, apt and PyPI); `down -v` keeps the images,
remove them with `docker image rm $(docker image ls -q 'dmc163load-*')` (use your project name).

Exit codes: 0 the verdict passed; 1 it failed, or the stack did not start or finish in time;
2 invalid arguments or a project that already exists; 128 + signal after SIGTERM or SIGINT.
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
import signal
import socket
import subprocess
import sys
import time
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from types import FrameType
from typing import Any, Protocol

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
# Published host ports: flag -> compose variable. PostgreSQL publishes none (psql runs through
# `compose exec`); the base file's defaults are the shared stand's ports.
PORT_FLAGS = {
    "backend_port": "BACKEND_PORT",
    "rabbitmq_port": "RABBITMQ_PORT",
    "rabbitmq_management_port": "RABBITMQ_MANAGEMENT_PORT",
    "redis_port": "REDIS_PORT",
    "stub_port": "LOAD_STUB_PORT",
}
# Pinned so the base file's credentials cannot come from anywhere else.
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

# End state of every `pull_request` receipt: pending until the webhook worker finishes it.
RECEIPTS_SQL = """
SELECT coalesce(json_object_agg(state, n), '{}'::json) FROM (
  SELECT CASE WHEN projected_at IS NOT NULL THEN 'projected'
              WHEN projection_failed_at IS NOT NULL THEN 'failed'
              WHEN projection_deferred_at IS NOT NULL THEN 'deferred'
              ELSE 'pending' END AS state,
         count(*) AS n
  FROM webhook_events WHERE event = 'pull_request' GROUP BY 1
) s
"""


def compose_project_name(name: str) -> str:
    """The project name compose derives from a directory name."""
    return re.sub(r"[^a-z0-9_-]", "", name.lower())


def project_error(project: str | None) -> str | None:
    """Why ``project`` may not be used, or None when it may be (still subject to existence)."""
    if not project:
        return "--project is required: the load stack runs under its own compose project"
    if not _PROJECT_NAME.fullmatch(project):
        return "--project must be lowercase letters, digits, '-' or '_'"
    if project == SHARED_PROJECT:
        return f"--project must differ from the shared local stand {SHARED_PROJECT!r}"
    if project == compose_project_name(REPO_ROOT.name):
        return f"--project must differ from {project!r}, this checkout's default compose project"
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


def stack_env(environ: Mapping[str, str], generated: Mapping[str, str]) -> dict[str, str]:
    """What compose sees: PATH, HOME, DOCKER_*, the pinned credentials and ``generated``."""
    kept = {
        name: value
        for name, value in environ.items()
        if name in ("PATH", "HOME") or name.startswith("DOCKER_")
    }
    return {**kept, **DATABASE_ENV, **generated}


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


@dataclass(frozen=True)
class Observed:
    """What the stub, the broker and the receipts reported after the Runs."""

    review_posts: Mapping[str, int]
    llm_calls: Mapping[str, int]
    receipts: Mapping[str, int]
    dlq_depth: int | None
    peak: int
    timed_out: bool


def verdict(
    metrics: Sequence[RunMetrics],
    observed: Observed,
    *,
    runs: int,
    workers: int,
    max_queue_wait_ms: float,
) -> dict[str, bool]:
    """Each rule of the scenario; it passes only when all are true."""
    prs = {str(n): 1 for n in range(1, runs + 1)}
    waits = [m.queue_wait_ms for m in metrics]
    return {
        "finished_in_time": not observed.timed_out,
        "all_succeeded": bool(metrics) and all(m.state == "succeeded" for m in metrics),
        "all_first_attempt": bool(metrics) and all(m.attempt == 1 for m in metrics),
        "one_run_per_pr": sorted(m.pr for m in metrics) == list(range(1, runs + 1)),
        "one_review_per_pr": dict(observed.review_posts) == prs,
        "one_review_call_per_run": observed.llm_calls.get("ReviewOutput") == runs,
        # Each PR got two deliveries: the first and its duplicate.
        "every_delivery_projected": dict(observed.receipts) == {"projected": 2 * runs},
        "all_workers_used": len({m.worker_id for m in metrics if m.worker_id}) == workers,
        "parallel": observed.peak >= min(runs, workers),
        "queue_wait_within_limit": bool(waits)
        and all(wait is not None and wait <= max_queue_wait_ms for wait in waits),
        "dlq_empty": observed.dlq_depth == 0,
    }


def throwaway_app_key() -> str:
    """A fresh RSA key for the App JWT; the stub never checks the signature."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()).decode()


def free_ports(count: int) -> list[int]:
    """Distinct loopback ports nothing listens on right now (a later bind can still race)."""
    ports: set[int] = set()
    while len(ports) < count:
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            ports.add(int(sock.getsockname()[1]))
    return sorted(ports)


class StackError(Exception):
    """A compose or HTTP step of the load stack failed; the message says which."""


class LoadStack(Protocol):
    """The load project as the scenario drives it (a fake in the tests)."""

    project: str

    def existing(self) -> list[str]: ...

    def up(self, workers: int) -> None: ...

    def seed(self) -> None: ...

    def post_delivery(self, body: bytes, headers: Mapping[str, str]) -> tuple[int, object]: ...

    def rows(self) -> list[dict[str, object]]: ...

    def receipts(self) -> dict[str, int]: ...

    def stub_stats(self) -> dict[str, Any]: ...

    def dlq_depth(self) -> int | None: ...

    def log_tail(self) -> None: ...

    def down(self) -> None: ...


class Stack:
    """The compose project of one load run; every command carries the same project and env."""

    def __init__(self, project: str, env: Mapping[str, str], ports: Mapping[str, str]) -> None:
        self.project = project
        self.env = dict(env)
        self.ports = dict(ports)

    def _run(
        self, *args: str, stdin: str | None = None, capture: bool = True, check: bool = True
    ) -> subprocess.CompletedProcess[str]:
        try:
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
        except subprocess.CalledProcessError as failed:
            detail = (failed.stderr or "").strip()[-2000:]
            raise StackError(
                f"docker compose {' '.join(args[:3])} exited {failed.returncode}: {detail}"
            ) from failed

    def existing(self) -> list[str]:
        containers = self._run("ps", "-aq").stdout.split()
        volumes = subprocess.run(
            [
                "docker",
                "volume",
                "ls",
                "-q",
                "--filter",
                f"label=com.docker.compose.project={self.project}",
            ],
            env=self.env,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.split()
        found = [f"{len(containers)} container(s)"] if containers else []
        return found + ([f"{len(volumes)} volume(s)"] if volumes else [])

    def up(self, workers: int) -> None:
        self._run("up", "-d", "--build", "--wait", "--scale", f"worker={workers}", capture=False)

    def _psql(self, sql: str, *, query: bool = False) -> str:
        args = ["exec", "-T", "postgres", "psql", "-U", "app", "-d", "app", "-v", "ON_ERROR_STOP=1"]
        return self._run(*args, *(["-tA"] if query else []), stdin=sql).stdout

    def seed(self) -> None:
        self._psql(SEED_SQL)

    def post_delivery(self, body: bytes, headers: Mapping[str, str]) -> tuple[int, object]:
        url = f"http://127.0.0.1:{self.ports['BACKEND_PORT']}/webhooks/github"
        response = httpx.post(url, content=body, headers=dict(headers), timeout=10)
        try:
            return response.status_code, response.json()
        except ValueError:
            return response.status_code, response.text[:200]

    def rows(self) -> list[dict[str, object]]:
        decoded = json.loads(self._psql(RUNS_SQL, query=True))
        assert isinstance(decoded, list)
        return decoded

    def receipts(self) -> dict[str, int]:
        decoded = json.loads(self._psql(RECEIPTS_SQL, query=True))
        assert isinstance(decoded, dict)
        return {str(state): int(count) for state, count in decoded.items()}

    def stub_stats(self) -> dict[str, Any]:
        url = f"http://127.0.0.1:{self.ports['LOAD_STUB_PORT']}/_stats"
        decoded = httpx.get(url, timeout=10).json()
        assert isinstance(decoded, dict)
        return decoded

    def dlq_depth(self) -> int | None:
        listed = self._run(
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
        # Every service: a broker or database that exits during `up` must show up here too.
        tail = self._run("logs", "--no-color", "--tail", "30", check=False)
        sys.stderr.write(tail.stdout + tail.stderr)

    def down(self) -> None:
        self._run("down", "-v", capture=False, check=False)


class Interrupted(Exception):
    def __init__(self, signum: int) -> None:
        super().__init__(signal.Signals(signum).name)
        self.signum = signum


def _interrupt(signum: int, frame: FrameType | None) -> None:
    raise Interrupted(signum)


def _fail(stack: LoadStack, message: str) -> int:
    print(f"FAIL: {message}", file=sys.stderr)
    stack.log_tail()
    return 1


def _send_deliveries(stack: LoadStack, runs: int, secret: str) -> str | None:
    """One delivery per PR, then one duplicate per PR; the first non-202 answer, if any."""
    for number in [*range(1, runs + 1), *range(1, runs + 1)]:
        status, answer = stack.post_delivery(*build_delivery(secret, number))
        if (status, answer) != (202, {"status": "pending"}):
            return f"PR {number}: {status} {answer}"
    return None


def scenario(stack: LoadStack, args: argparse.Namespace, secret: str) -> int:
    began = time.monotonic()
    try:
        stack.up(args.workers)
    except StackError as failed:
        return _fail(stack, f"the load stack did not start: {failed}")
    stack_seconds = round(time.monotonic() - began, 1)
    stack.seed()

    sent_at = time.monotonic()
    rejected = _send_deliveries(stack, args.runs, secret)
    if rejected is not None:
        return _fail(stack, f"a delivery was not accepted: {rejected}")

    rows: list[dict[str, object]] = []
    receipts: dict[str, int] = {}
    timed_out = True
    deadline = sent_at + args.timeout
    while time.monotonic() < deadline:
        rows, receipts = stack.rows(), stack.receipts()
        if (
            len(rows) >= args.runs
            and all(row["state"] in TERMINAL_STATES for row in rows)
            and sum(receipts.values()) == 2 * args.runs
            and "pending" not in receipts
        ):
            timed_out = False
            break
        time.sleep(args.poll_interval)
    runs_seconds = None if timed_out else round(time.monotonic() - sent_at, 1)
    if not timed_out:
        # A late duplicate Run would show up here.
        time.sleep(args.settle)
        rows, receipts = stack.rows(), stack.receipts()

    stats = stack.stub_stats()
    metrics = run_metrics(rows)
    observed = Observed(
        review_posts=stats["review_posts"],
        llm_calls=stats["llm_calls"],
        receipts=receipts,
        dlq_depth=stack.dlq_depth(),
        peak=max_parallel(rows),
        timed_out=timed_out,
    )
    checks = verdict(
        metrics,
        observed,
        runs=args.runs,
        workers=args.workers,
        max_queue_wait_ms=args.max_queue_wait_ms,
    )
    waits = [m.queue_wait_ms for m in metrics if m.queue_wait_ms is not None]
    report: dict[str, object] = {
        "project": stack.project,
        "runs": args.runs,
        "workers": args.workers,
        "duplicate_deliveries": args.runs,
        "llm_delay_ms": args.llm_delay_ms,
        "stack_up_s": stack_seconds,
        "deliveries_to_terminal_s": runs_seconds,
        "timed_out": timed_out,
        "per_run": [asdict(m) for m in metrics],
        "distinct_worker_ids": len({m.worker_id for m in metrics if m.worker_id}),
        "max_parallel": observed.peak,
        "max_queue_wait_ms": max(waits) if waits else None,
        "queue_wait_limit_ms": args.max_queue_wait_ms,
        "review_posts": dict(observed.review_posts),
        "llm_calls": dict(observed.llm_calls),
        "receipts": dict(observed.receipts),
        "unhandled_stub_requests": stats["unhandled"],
        "dlq_depth": observed.dlq_depth,
        "checks": checks,
        "passed": all(checks.values()),
    }
    print_report(report, metrics)
    sys.stdout.flush()  # before the teardown output on stderr
    if args.report_json is not None:
        args.report_json.write_text(json.dumps(report, indent=2) + "\n")
    if not report["passed"]:
        failed_checks = ", ".join(name for name, passed in checks.items() if not passed)
        return _fail(stack, f"the verdict failed: {failed_checks}")
    return 0


def execute(stack: LoadStack, args: argparse.Namespace, secret: str) -> int:
    """Refuse an existing project, run the scenario, and remove only what this run created."""
    try:
        leftovers = stack.existing()
    except (StackError, subprocess.CalledProcessError, OSError) as failed:
        print(f"error: cannot inspect compose project {stack.project!r}: {failed}", file=sys.stderr)
        return 2
    if leftovers:
        print(
            f"error: compose project {stack.project!r} already has {' and '.join(leftovers)}; "
            "it is not this run's to remove, pick another --project",
            file=sys.stderr,
        )
        return 2
    signals = (signal.SIGTERM, signal.SIGINT)
    previous = {sig: signal.signal(sig, _interrupt) for sig in signals}
    created = False
    try:
        # Set before `up`: a failed `up` still leaves containers, volumes and a network behind.
        created = True
        return scenario(stack, args, secret)
    except Interrupted as stop:
        print(f"interrupted by {stop}; removing the load stack", file=sys.stderr)
        return 128 + stop.signum
    except (StackError, httpx.HTTPError) as failed:
        return _fail(stack, str(failed) or repr(failed))
    finally:
        try:
            if created and not args.keep:
                # Children inherit SIG_IGN, so a second Ctrl-C cannot cut `down -v` short.
                for sig in signals:
                    signal.signal(sig, signal.SIG_IGN)
                stack.down()
            elif created:
                print(f"--keep: the stack {stack.project!r} is still up", file=sys.stderr)
        finally:
            for sig, handler in previous.items():
                signal.signal(sig, handler if handler is not None else signal.SIG_DFL)


def print_report(report: Mapping[str, object], metrics: Sequence[RunMetrics]) -> None:
    def ms(value: float | None) -> str:
        return "-" if value is None else f"{value:.0f}"

    print(
        f"parallel Runs: {report['runs']} PRs on {report['workers']} workers (+ "
        f"{report['duplicate_deliveries']} duplicate deliveries), LLM delay "
        f"{report['llm_delay_ms']} ms, stack up in {report['stack_up_s']} s, deliveries to "
        f"terminal in {report['deliveries_to_terminal_s']} s"
        + (" (TIMED OUT)" if report["timed_out"] else "")
    )
    print("pr  state      attempt  projection_ms  queue_wait_ms  broker_ms  total_ms  worker_id")
    for m in metrics:
        print(
            f"{m.pr:<3} {m.state:<10} {m.attempt:<8} {ms(m.projection_ms):>13}  "
            f"{ms(m.queue_wait_ms):>13}  {ms(m.broker_ms):>9}  {ms(m.total_ms):>8}  {m.worker_id}"
        )
    print(
        f"distinct workers: {report['distinct_worker_ids']}, max parallel: "
        f"{report['max_parallel']}, max queue wait: {report['max_queue_wait_ms']} ms (limit "
        f"{report['queue_wait_limit_ms']}), review POSTs: {report['review_posts']}, LLM calls: "
        f"{report['llm_calls']}, receipts: {report['receipts']}, DLQ depth: {report['dlq_depth']}"
    )
    if report["unhandled_stub_requests"]:
        print(f"unhandled stub requests: {report['unhandled_stub_requests']}")
    checks = report["checks"]
    assert isinstance(checks, dict)
    print("verdict: " + ("PASS" if report["passed"] else "FAIL"))
    for name, passed in checks.items():
        print(f"  {'ok ' if passed else 'FAIL'} {name}")


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--project", help="compose project name, new for this run")
    parser.add_argument("--runs", type=int, default=5, help=f"PRs (1-{MAX_RUNS})")
    parser.add_argument("--workers", type=int, help="worker replicas (1..--runs, default --runs)")
    parser.add_argument("--llm-delay-ms", type=int, default=3000, help="delay of each LLM answer")
    parser.add_argument(
        "--max-queue-wait-ms", type=float, default=30000.0, help="verdict limit per Run"
    )
    parser.add_argument("--timeout", type=float, default=300.0, help="seconds to terminal Runs")
    parser.add_argument("--settle", type=float, default=5.0, help="seconds before the recount")
    parser.add_argument("--poll-interval", type=float, default=1.0, help="seconds between polls")
    parser.add_argument("--keep", action="store_true", help="leave the stack up (no down -v)")
    parser.add_argument("--report-json", type=Path, help="write the report as JSON here")
    for flag, variable in PORT_FLAGS.items():
        parser.add_argument(f"--{flag.replace('_', '-')}", type=int, help=f"host {variable}")
    args = parser.parse_args(argv)
    if not 1 <= args.runs <= MAX_RUNS:
        parser.error(f"--runs must be from 1 to {MAX_RUNS}")
    if args.workers is None:
        args.workers = args.runs
    if not 1 <= args.workers <= args.runs:
        parser.error("--workers must be from 1 to --runs (each worker must get a Run)")
    if args.llm_delay_ms < 0 or args.settle < 0 or args.poll_interval < 0:
        parser.error("--llm-delay-ms, --settle and --poll-interval must be >= 0")
    if args.timeout <= 0 or args.max_queue_wait_ms <= 0:
        parser.error("--timeout and --max-queue-wait-ms must be > 0")
    return args


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    error = project_error(args.project)
    if error is not None:
        print(f"error: {error}", file=sys.stderr)
        return 2
    if shutil.which("docker") is None:
        print("error: docker is not on PATH", file=sys.stderr)
        return 2

    fixed = {variable: getattr(args, flag) for flag, variable in PORT_FLAGS.items()}
    free = iter(free_ports(len(fixed)))
    ports = {variable: str(port or next(free)) for variable, port in fixed.items()}
    secret = secrets.token_hex(32)
    generated = {
        **ports,
        "LOAD_RUNS": str(args.runs),
        "LOAD_LLM_DELAY_MS": str(args.llm_delay_ms),
        "LOAD_WEBHOOK_SECRET": secret,
        "LOAD_GITHUB_APP_PRIVATE_KEY": throwaway_app_key(),
    }
    stack = Stack(args.project, stack_env(os.environ, generated), ports)
    print(f"load stack {args.project!r} on loopback ports {ports}", file=sys.stderr)
    return execute(stack, args, secret)


if __name__ == "__main__":
    raise SystemExit(main())
