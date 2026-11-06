"""The dashboard profile command: guards, percentiles and a tiny end-to-end run (#456)."""

from __future__ import annotations

import contextlib
import importlib.util
import io
import os
import re
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any

import psycopg
import pytest
from sqlalchemy.engine import URL, make_url

from app.modules.reviews.application.saved_review_result import read_saved_review_result

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "dashboard_profile.py"
_TINY = ["--prs-per-day", "5", "--history-days", "40", "--requests", "3"]


@pytest.fixture(scope="module")
def profile() -> Iterator[ModuleType]:
    spec = importlib.util.spec_from_file_location("dashboard_profile", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
        yield module
    finally:
        sys.modules.pop(spec.name, None)


@pytest.mark.parametrize(
    ("samples", "q", "expected"),
    [
        ([1.0], 95, 1.0),
        ([3.0, 1.0, 2.0, 4.0], 50, 2.0),
        ([float(n) for n in range(1, 101)], 95, 95.0),
        ([float(n) for n in range(1, 101)], 100, 100.0),
    ],
)
def test_percentile_is_nearest_rank(
    profile: ModuleType, samples: list[float], q: float, expected: float
) -> None:
    assert profile.percentile(samples, q) == expected


@pytest.mark.parametrize(
    "server_url",
    [
        "postgresql+psycopg://app:app@staging.example.test:5432/app",
        "postgresql+psycopg://app:app@10.0.0.5:5432/app",
        "postgresql+psycopg://app:app@/app",
        "not a url",
    ],
)
def test_a_server_that_is_not_loopback_is_refused_before_any_connection(
    profile: ModuleType, server_url: str, capsys: pytest.CaptureFixture[str]
) -> None:
    assert profile.main(["--server-url", server_url]) == 2
    assert "loopback" in capsys.readouterr().err


@pytest.mark.parametrize(
    "arguments",
    [
        ["--prs-per-day", "0"],
        ["--history-days", "0"],
        ["--requests", "0"],
    ],
)
def test_a_size_that_is_not_positive_is_refused(
    profile: ModuleType, arguments: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    server_url = "postgresql+psycopg://app:app@127.0.0.1:1/app"
    assert profile.main(["--server-url", server_url, *arguments]) == 2
    assert "positive" in capsys.readouterr().err


@dataclass(frozen=True)
class TinyRun:
    code: int
    stdout: str
    server_url: URL
    database: str

    def connect(self) -> psycopg.Connection[tuple[Any, ...]]:
        url = self.server_url.set(drivername="postgresql", database=self.database)
        return psycopg.connect(url.render_as_string(hide_password=False))

    def printed(self, name: str) -> int:
        match = re.search(rf"\b{name}=(\d+)", self.stdout)
        assert match is not None, self.stdout
        return int(match.group(1))


def _run_profile(profile: ModuleType, server_url: str, *flags: str) -> tuple[int, str, str]:
    stdout = io.StringIO()
    with contextlib.redirect_stdout(stdout):
        code = profile.main(["--server-url", server_url, *_TINY, *flags])
    match = re.search(r"database: (dashboard_profile_[0-9a-f]+)", stdout.getvalue())
    assert match is not None, stdout.getvalue()
    return code, stdout.getvalue(), match.group(1)


@pytest.fixture(scope="module")
def tiny_run(profile: ModuleType) -> Iterator[TinyRun]:
    """One end-to-end run on a tiny volume, its database kept for the assertions."""
    server_url = os.environ.get("TEST_DATABASE_URL")
    if server_url is None:
        pytest.skip("set TEST_DATABASE_URL to run PostgreSQL integration tests")
    code, stdout, database = _run_profile(profile, server_url, "--keep")
    run = TinyRun(code, stdout, make_url(server_url), database)
    try:
        yield run
    finally:
        admin = run.server_url.set(drivername="postgresql")
        with psycopg.connect(admin.render_as_string(hide_password=False), autocommit=True) as conn:
            conn.execute(f'DROP DATABASE IF EXISTS "{database}" WITH (FORCE)')


@pytest.mark.integration
def test_the_printed_volume_is_what_the_database_holds(tiny_run: TinyRun) -> None:
    assert tiny_run.code == 0, tiny_run.stdout
    with tiny_run.connect() as connection:
        for table, name in (
            ("code_changes", "prs"),
            ("runs", "runs"),
            ("findings", "findings"),
            ("usage_events", "usage_events"),
        ):
            row = connection.execute(f"SELECT count(*) FROM {table}").fetchone()
            assert row is not None
            assert row[0] == tiny_run.printed(name), table
    assert tiny_run.printed("prs") == 5 * 40
    assert tiny_run.printed("runs") > tiny_run.printed("prs")
    assert tiny_run.printed("findings") > 0
    assert tiny_run.printed("usage_events") > 0


@pytest.mark.integration
def test_saved_results_take_the_production_read_path(tiny_run: TinyRun) -> None:
    """A result the app rejects would profile the fallback read instead of the normal one."""
    with tiny_run.connect() as connection:
        rows = connection.execute(
            """
            SELECT a.response, r.head_sha,
                   array_remove(array_agg(f.severity::text), NULL)
            FROM run_actions a
            JOIN runs r ON r.id = a.run_id
            LEFT JOIN findings f
              ON f.run_id = r.id AND f.published AND f.drop_reason IS NULL
            WHERE a.tool = 'review.postprocess'
            GROUP BY a.id, r.head_sha
            """
        ).fetchall()
        succeeded = connection.execute(
            "SELECT count(*) FROM runs WHERE state = 'succeeded'"
        ).fetchone()
    assert succeeded is not None
    assert rows
    # Only the summary-only share has no saved result.
    assert len(rows) >= 0.9 * succeeded[0]
    for response, head_sha, severities in rows:
        assert (
            read_saved_review_result(response, head_sha=head_sha, severities=severities) is not None
        )


@pytest.mark.integration
def test_the_measured_user_sees_only_part_of_the_data(tiny_run: TinyRun) -> None:
    with tiny_run.connect() as connection:
        repositories = connection.execute("SELECT count(*) FROM repositories").fetchone()
        visible = connection.execute(
            "SELECT count(*) FROM github_user_repository_access"
        ).fetchone()
    assert repositories == (25,)
    assert visible == (20,)


@pytest.mark.integration
def test_the_same_seed_gives_the_same_rows_and_an_unkept_database_is_dropped(
    profile: ModuleType, tiny_run: TinyRun
) -> None:
    server_url = tiny_run.server_url.render_as_string(hide_password=False)
    code, stdout, database = _run_profile(profile, server_url)
    assert code == 0, stdout
    digest = re.compile(r"rows digest: ([0-9a-f]{32})")
    first, second = digest.search(tiny_run.stdout), digest.search(stdout)
    assert first is not None and second is not None
    assert first.group(1) == second.group(1)
    admin = tiny_run.server_url.set(drivername="postgresql")
    with psycopg.connect(admin.render_as_string(hide_password=False)) as connection:
        row = connection.execute(
            "SELECT count(*) FROM pg_database WHERE datname = %s", (database,)
        ).fetchone()
    assert row == (0,)
