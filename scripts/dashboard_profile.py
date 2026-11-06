#!/usr/bin/env python3
"""Profile of the dashboard endpoints on a declared data volume (#456)."""

from __future__ import annotations

import argparse
import ipaddress
import json
import math
import random
import sys
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import psycopg
from alembic.config import Config
from sqlalchemy import create_engine
from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import ArgumentError

from alembic import command
from app.modules.reviews.application.run_coverage import CoverageStatus, RunCoverage
from app.modules.reviews.application.verdict import coverage_verdict, severity_counts

_USER_ID = 42
# Repositories per Workspace and whether the measured user sees them: a real visibility filter.
_WORKSPACES = ((10, True), (10, True), (5, False))
# SD §13: five parallel Runs, so at most five are active at the end of the history.
_ACTIVE_RUNS = 5
_SECOND_RUN_SHARE = 0.5
_TERMINAL_STATES = ("succeeded", "failed", "cancelled", "skipped")
_TERMINAL_WEIGHTS = (85, 5, 6, 4)
# A summary-only Run has no saved result: its share keeps the fallback read in the profile.
_SUMMARY_ONLY_SHARE = 0.02
_MAX_FINDINGS = 8
_PUBLISHED_SHARE = 0.7
_SEVERITIES = ("critical", "high", "medium", "low", "info")
_SEVERITY_WEIGHTS = (5, 15, 40, 30, 10)
_CATEGORIES = ("security", "correctness", "performance", "readability")
_USAGE_OPERATIONS = ("review.conventions", "review", "review.verify")
_ACTION_TOOLS = (
    "diff.snapshot",
    "policy.load",
    "context.build",
    "llm.conventions",
    "llm.review",
    "llm.verify",
)
_FILES_PER_RUN = 5
# Fixed texts of typical length: rows are as wide as real ones, generating them costs nothing.
_DESCRIPTION = "Refactor the review pipeline and cover the new branch with tests. " * 6
_REVIEW_BODY = "Summary of the review with its findings and the overall verdict. " * 18
_FINDING_BODY = "The branch does not handle an empty result, so the caller fails later. " * 4
_PATCH = "@@ -1,5 +1,7 @@\n-old line of the changed file\n+new line of the changed file\n" * 8
_COLUMNS: dict[str, tuple[str, ...]] = {
    # In foreign-key order: COPY runs through this mapping.
    "workspaces": ("id", "name", "daily_budget_usd"),
    "provider_installations": ("id", "workspace_id", "provider", "external_id", "metadata"),
    "github_user_profiles": ("id", "login", "name"),
    "github_user_workspace_access": ("github_user_id", "workspace_id"),
    "github_user_repository_access": (
        "github_user_id",
        "provider_installation_id",
        "repository_external_id",
    ),
    "prompt_versions": ("id", "key", "version", "content", "checksum", "is_active"),
    "repositories": (
        "id",
        "provider_installation_id",
        "external_id",
        "full_name",
        "default_branch",
        "web_url",
    ),
    "rule_versions": ("id", "repository_id", "version", "rules", "checksum", "is_active"),
    "code_changes": (
        "id",
        "repository_id",
        "external_id",
        "external_number",
        "title",
        "description",
        "author_login",
        "source_branch",
        "target_branch",
        "base_sha",
        "head_sha",
        "state",
        "web_url",
        "provider_updated_at",
        "created_at",
        "updated_at",
    ),
    "runs": (
        "id",
        "code_change_id",
        "origin_provider_installation_id",
        "base_sha",
        "base_ref",
        "head_sha",
        "state",
        "trigger",
        "triggered_by",
        "idempotency_key",
        "engine",
        "rule_version_id",
        "prompt_version_id",
        "attempt",
        "message_published_at",
        "available_at",
        "review_body",
        "error_code",
        "started_at",
        "finished_at",
        "created_at",
        "updated_at",
    ),
    "code_change_diffs": (
        "id",
        "run_id",
        "code_change_id",
        "head_sha",
        "filename",
        "patch",
        "additions",
        "deletions",
        "changes",
        "summary_only",
        "created_at",
    ),
    "context_payloads": ("id", "run_id", "schema_version", "summary", "created_at"),
    "run_actions": (
        "id",
        "run_id",
        "index",
        "tool",
        "request",
        "response",
        "started_at",
        "duration_ms",
    ),
    "findings": (
        "id",
        "run_id",
        "file_path",
        "line_start",
        "severity",
        "confidence",
        "category",
        "title",
        "body",
        "published",
        "inline_comment",
        "drop_reason",
        "created_at",
    ),
    "usage_events": (
        "id",
        "run_id",
        "workspace_id",
        "provider",
        "model",
        "operation",
        "tokens_in",
        "tokens_out",
        "cost_usd",
        "created_at",
    ),
}

type Rows = dict[str, list[tuple[Any, ...]]]


class Refused(Exception):
    """An argument the run must not start with: exit 2, nothing touched."""


@dataclass(frozen=True)
class Volume:
    prs_per_day: int
    history_days: int

    @property
    def prs(self) -> int:
        return self.prs_per_day * self.history_days


@dataclass(frozen=True)
class _Repository:
    id: UUID
    installation_id: UUID
    workspace_id: UUID
    rule_version_id: UUID
    external_id: int
    full_name: str


def percentile(samples: Sequence[float], q: float) -> float:
    """Nearest-rank percentile: the smallest sample with at least q% of samples at or below it."""
    ordered = sorted(samples)
    rank = max(1, math.ceil(q / 100 * len(ordered)))
    return ordered[rank - 1]


def loopback_server_url(value: str) -> URL:
    """The maintenance URL of a local server; a stand must not get here, even by mistake."""
    try:
        url = make_url(value)
    except ArgumentError as error:
        raise Refused(f"--server-url is not a loopback database URL: {error}") from error
    host = url.host or ""
    if host != "localhost":
        try:
            if not ipaddress.ip_address(host).is_loopback:
                raise Refused(f"--server-url host {host} is not loopback")
        except ValueError as error:
            raise Refused(f"--server-url host {host!r} is not loopback") from error
    return url


def _conninfo(url: URL) -> str:
    return url.set(drivername="postgresql").render_as_string(hide_password=False)


@contextmanager
def own_database(server_url: URL, *, keep: bool) -> Iterator[URL]:
    """A new database on the server, the only one this command writes to or drops."""
    name = f"dashboard_profile_{uuid4().hex[:12]}"
    with psycopg.connect(_conninfo(server_url), autocommit=True) as connection:
        connection.execute(f'CREATE DATABASE "{name}"')
    print(f"database: {name}", flush=True)
    try:
        yield server_url.set(database=name)
    finally:
        if keep:
            print(f'kept; to remove it: DROP DATABASE "{name}" WITH (FORCE)', flush=True)
        else:
            with psycopg.connect(_conninfo(server_url), autocommit=True) as connection:
                connection.execute(f'DROP DATABASE "{name}" WITH (FORCE)')


def _migrate(database_url: URL) -> None:
    engine = create_engine(database_url)
    try:
        with engine.connect() as connection:
            config = Config("alembic.ini")
            config.attributes["connection"] = connection
            command.upgrade(config, "head")
            connection.commit()
    finally:
        engine.dispose()


def _uid(rng: random.Random) -> UUID:
    return UUID(int=rng.getrandbits(128), version=4)


def _hex(rng: random.Random, length: int) -> str:
    return f"{rng.getrandbits(length * 4):0{length}x}"


def _tenants(rng: random.Random, rows: Rows, prompt_id: UUID) -> list[_Repository]:
    repositories: list[_Repository] = []
    rows["github_user_profiles"].append((_USER_ID, "profile-user", "Profile User"))
    rows["prompt_versions"].append(
        (prompt_id, "review.system", 1, "system prompt", _hex(rng, 64), True)
    )
    for position, (count, visible) in enumerate(_WORKSPACES):
        workspace_id, installation_id = _uid(rng), _uid(rng)
        rows["workspaces"].append((workspace_id, f"workspace-{position}", Decimal(50)))
        rows["provider_installations"].append(
            (installation_id, workspace_id, "github", 1000 + position, "{}")
        )
        if visible:
            rows["github_user_workspace_access"].append((_USER_ID, workspace_id))
        for _ in range(count):
            external_id = 2000 + len(repositories)
            name = f"profile/repo-{len(repositories):02d}"
            repository = _Repository(
                _uid(rng), installation_id, workspace_id, _uid(rng), external_id, name
            )
            repositories.append(repository)
            rows["repositories"].append(
                (
                    repository.id,
                    installation_id,
                    external_id,
                    name,
                    "main",
                    f"https://github.test/{name}",
                )
            )
            rows["rule_versions"].append(
                (repository.rule_version_id, repository.id, 1, "[]", _hex(rng, 64), True)
            )
            if visible:
                rows["github_user_repository_access"].append(
                    (_USER_ID, installation_id, external_id)
                )
    return repositories


def _saved_result(rng: random.Random, head_sha: str, severities: Sequence[str]) -> str:
    """The ``review.postprocess`` response of store_review_output, built with the app's helpers."""
    coverage = RunCoverage(
        CoverageStatus.COMPLETE,
        _FILES_PER_RUN,
        head_sha=head_sha,
        diff_digest=_hex(rng, 64),
        snapshot_digest=_hex(rng, 64),
        total_files=_FILES_PER_RUN,
        file_list_complete=True,
    )
    return json.dumps(
        {
            "inline": len(severities),
            "body_only": 0,
            "omitted_body_only_count": 0,
            "dropped": [],
            "verdict": coverage_verdict(severities, CoverageStatus.COMPLETE),
            "coverage": coverage.to_json(),
            "severity_counts": severity_counts(severities),
            "findings_hash": _hex(rng, 64),
            "summary": {
                "problem": "The branch does not handle an empty result.",
                "done_well": "The change is small and covered by tests.",
                "effort": "small",
            },
        }
    )


def _findings(rng: random.Random, rows: Rows, run_id: UUID, created_at: datetime) -> list[str]:
    """Findings of one Run; returns the severities the verdict counts."""
    counted: list[str] = []
    for _ in range(rng.randint(0, _MAX_FINDINGS)):
        severity = rng.choices(_SEVERITIES, _SEVERITY_WEIGHTS)[0]
        published = rng.random() < _PUBLISHED_SHARE
        if published:
            counted.append(severity)
        rows["findings"].append(
            (
                _uid(rng),
                run_id,
                f"src/module_{rng.randrange(40)}/file_{rng.randrange(20)}.py",
                rng.randint(1, 800),
                severity,
                Decimal("0.80"),
                rng.choice(_CATEGORIES),
                "The branch does not handle an empty result",
                _FINDING_BODY,
                published,
                published,
                None if published else "below_threshold",
                created_at,
            )
        )
    return counted


def _run_children(
    rng: random.Random,
    rows: Rows,
    *,
    run_id: UUID,
    change_id: UUID,
    workspace_id: UUID,
    head_sha: str,
    state: str,
    created_at: datetime,
) -> None:
    summary_only = state == "succeeded" and rng.random() < _SUMMARY_ONLY_SHARE
    for position in range(_FILES_PER_RUN):
        rows["code_change_diffs"].append(
            (
                _uid(rng),
                run_id,
                change_id,
                head_sha,
                f"src/module_{position}/file_{rng.randrange(20)}.py",
                _PATCH,
                8,
                8,
                16,
                summary_only,
                created_at,
            )
        )
    summary = (
        {"status": "summary_only", "reason": "too_many_lines"}
        if summary_only
        else {"status": "ready", "reviewable_lines": 120, "files": _FILES_PER_RUN}
    )
    rows["context_payloads"].append((_uid(rng), run_id, 1, json.dumps(summary), created_at))
    for index, tool in enumerate(_ACTION_TOOLS):
        rows["run_actions"].append(
            (_uid(rng), run_id, index, tool, '{"attempt": 1}', '{"ok": true}', created_at, 900)
        )
    for operation in _USAGE_OPERATIONS:
        rows["usage_events"].append(
            (
                _uid(rng),
                run_id,
                workspace_id,
                "eurouter",
                "mistral-small-4",
                operation,
                rng.randint(2000, 30000),
                rng.randint(200, 2000),
                Decimal(rng.randint(1000, 60000)) / 1_000_000,
                created_at,
            )
        )
    if state == "succeeded" and not summary_only:
        severities = _findings(rng, rows, run_id, created_at)
        rows["run_actions"].append(
            (
                _uid(rng),
                run_id,
                len(_ACTION_TOOLS),
                "review.postprocess",
                '{"min_confidence": 0.7, "max_inline": 10, "severity_threshold": "low"}',
                _saved_result(rng, head_sha, severities),
                created_at,
                12,
            )
        )


def _run(
    rng: random.Random,
    rows: Rows,
    *,
    repository: _Repository,
    prompt_id: UUID,
    change_id: UUID,
    base_sha: str,
    head_sha: str,
    state: str,
    created_at: datetime,
) -> None:
    run_id = _uid(rng)
    active = state in ("queued", "running")
    finished_at = None if active else created_at + timedelta(seconds=rng.randint(30, 240))
    rows["runs"].append(
        (
            run_id,
            change_id,
            repository.installation_id,
            base_sha,
            "main",
            head_sha,
            state,
            "webhook",
            "octocat",
            _hex(rng, 64),
            "fast",
            repository.rule_version_id,
            prompt_id,
            1,
            created_at,
            created_at,
            _REVIEW_BODY if state == "succeeded" else None,
            "llm_unavailable" if state == "failed" else None,
            None if state == "queued" else created_at + timedelta(seconds=5),
            finished_at,
            created_at,
            finished_at or created_at,
        )
    )
    if not active and state != "skipped":
        _run_children(
            rng,
            rows,
            run_id=run_id,
            change_id=change_id,
            workspace_id=repository.workspace_id,
            head_sha=head_sha,
            state=state,
            created_at=created_at,
        )


def generate_rows(volume: Volume, seed: int, now: datetime) -> Rows:
    """Every row of the volume; the same seed gives the same ids and states."""
    rng = random.Random(seed)
    rows: Rows = {table: [] for table in _COLUMNS}
    prompt_id = _uid(rng)
    repositories = _tenants(rng, rows, prompt_id)
    history = timedelta(days=volume.history_days)
    active_from = volume.prs - min(_ACTIVE_RUNS, volume.prs // 10)
    for number in range(volume.prs):
        repository = rng.choice(repositories)
        change_id = _uid(rng)
        created_at = now - history * ((volume.prs - number - rng.random()) / volume.prs)
        base_sha = _hex(rng, 40)
        heads = [_hex(rng, 40) for _ in range(2 if rng.random() < _SECOND_RUN_SHARE else 1)]
        rows["code_changes"].append(
            (
                change_id,
                repository.id,
                number + 1,
                number + 1,
                f"Change number {number + 1} of the review pipeline",
                _DESCRIPTION,
                "octocat",
                f"feature/{number + 1}",
                "main",
                base_sha,
                heads[-1],
                rng.choices(("merged", "closed", "open"), (75, 10, 15))[0],
                f"https://github.test/{repository.full_name}/pull/{number + 1}",
                created_at,
                created_at,
                created_at,
            )
        )
        for position, head_sha in enumerate(heads):
            last = position == len(heads) - 1
            if last and number >= active_from:
                state = "running" if number % 2 else "queued"
            else:
                state = rng.choices(_TERMINAL_STATES, _TERMINAL_WEIGHTS)[0]
            _run(
                rng,
                rows,
                repository=repository,
                prompt_id=prompt_id,
                change_id=change_id,
                base_sha=base_sha,
                head_sha=head_sha,
                state=state,
                created_at=min(
                    now, created_at + timedelta(minutes=position * rng.randint(10, 600))
                ),
            )
    return rows


def seed(database_url: URL, volume: Volume, seed_value: int, now: datetime) -> None:
    rows = generate_rows(volume, seed_value, now)
    with psycopg.connect(_conninfo(database_url)) as connection, connection.cursor() as cursor:
        for table, columns in _COLUMNS.items():
            quoted = ", ".join(f'"{column}"' for column in columns)
            with cursor.copy(f"COPY {table} ({quoted}) FROM STDIN") as copy:
                for row in rows[table]:
                    copy.write_row(row)
    with psycopg.connect(_conninfo(database_url), autocommit=True) as connection:
        connection.execute("VACUUM (ANALYZE)")


def _scalar(connection: psycopg.Connection[tuple[Any, ...]], statement: str) -> Any:
    row = connection.execute(statement).fetchone()
    assert row is not None
    return row[0]


def print_volume(database_url: URL) -> None:
    with psycopg.connect(_conninfo(database_url)) as connection:
        counts = {
            name: _scalar(connection, f"SELECT count(*) FROM {table}")
            for name, table in (
                ("prs", "code_changes"),
                ("runs", "runs"),
                ("findings", "findings"),
                ("usage_events", "usage_events"),
                ("run_actions", "run_actions"),
                ("code_change_diffs", "code_change_diffs"),
            )
        }
        window = _scalar(
            connection, "SELECT count(*) FROM runs WHERE created_at >= now() - interval '30 days'"
        )
        digest = _scalar(
            connection, "SELECT md5(string_agg(id::text || state::text, ',' ORDER BY id)) FROM runs"
        )
    print("volume: " + " ".join(f"{name}={count}" for name, count in counts.items()))
    print(f"window_30d: window_runs={window}")
    print(f"rows digest: {digest}", flush=True)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server-url", required=True, help="loopback PostgreSQL maintenance URL")
    parser.add_argument("--prs-per-day", type=int, default=100)
    parser.add_argument("--history-days", type=int, default=365)
    parser.add_argument("--requests", type=int, default=200, help="timed requests per scenario")
    parser.add_argument("--seed", type=int, default=456)
    parser.add_argument("--keep", action="store_true", help="do not drop the database at the end")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        server_url = loopback_server_url(arguments.server_url)
        for name in ("prs_per_day", "history_days", "requests"):
            if getattr(arguments, name) < 1:
                raise Refused(f"--{name.replace('_', '-')} must be positive")
    except Refused as error:
        print(f"dashboard_profile: {error}", file=sys.stderr)
        return 2
    volume = Volume(arguments.prs_per_day, arguments.history_days)
    with own_database(server_url, keep=arguments.keep) as database_url:
        _migrate(database_url)
        seed(database_url, volume, arguments.seed, datetime.now(UTC))
        print_volume(database_url)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
