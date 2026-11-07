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
_LOOPBACK = "postgresql+psycopg://app:app@127.0.0.1:1/app"


@pytest.fixture(scope="module", autouse=True)
def _no_libpq_redirect() -> Iterator[None]:
    """An exported PGHOSTADDR or PGSERVICE refuses every run; only the guard test sets one."""
    with pytest.MonkeyPatch.context() as patch:
        for variable in ("PGHOSTADDR", "PGSERVICE"):
            patch.delenv(variable, raising=False)
        yield


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
        # libpq takes these over the authority, so the server would not be the one checked.
        "postgresql+psycopg://app:app@127.0.0.1:5432/app?host=staging.example.test",
        "postgresql+psycopg://app:app@127.0.0.1:5432/app?hostaddr=10.0.0.5",
        "postgresql+psycopg://app:app@127.0.0.1:5432/app?service=staging",
    ],
)
def test_a_server_that_is_not_loopback_is_refused_before_any_connection(
    profile: ModuleType, server_url: str, capsys: pytest.CaptureFixture[str]
) -> None:
    assert profile.main(["--server-url", server_url]) == 2
    assert "loopback" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        (["--prs-per-day", "0"], "positive"),
        (["--history-days", "0"], "positive"),
        (["--requests", "0"], "positive"),
    ],
)
def test_an_invalid_argument_is_refused_before_any_connection(
    profile: ModuleType, arguments: list[str], message: str, capsys: pytest.CaptureFixture[str]
) -> None:
    assert profile.main(["--server-url", _LOOPBACK, *arguments]) == 2
    assert message in capsys.readouterr().err


@dataclass(frozen=True)
class Finished:
    code: int
    stdout: str
    stderr: str
    database: str


@dataclass(frozen=True)
class TinyRun:
    code: int
    stdout: str
    server_url: URL
    database: str
    explain: Path

    def connect(self) -> psycopg.Connection[tuple[Any, ...]]:
        url = self.server_url.set(drivername="postgresql", database=self.database)
        return psycopg.connect(url.render_as_string(hide_password=False))

    def printed(self, name: str) -> int:
        match = re.search(rf"\b{name}=(\d+)", self.stdout)
        assert match is not None, self.stdout
        return int(match.group(1))


def _run_profile(profile: ModuleType, server_url: str, *flags: str) -> Finished:
    stdout, stderr = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        code = profile.main(["--server-url", server_url, *_TINY, *flags])
    match = re.search(r"database: (dashboard_profile_[0-9a-f]+)", stdout.getvalue())
    assert match is not None, stdout.getvalue()
    return Finished(code, stdout.getvalue(), stderr.getvalue(), match.group(1))


def _admin(server_url: str) -> str:
    return make_url(server_url).set(drivername="postgresql").render_as_string(hide_password=False)


def _database_exists(server_url: str, database: str) -> bool:
    with psycopg.connect(_admin(server_url)) as connection:
        row = connection.execute(
            "SELECT count(*) FROM pg_database WHERE datname = %s", (database,)
        ).fetchone()
    return row != (0,)


def _drop_database(server_url: str, database: str) -> None:
    with psycopg.connect(_admin(server_url), autocommit=True) as connection:
        connection.execute(f'DROP DATABASE IF EXISTS "{database}" WITH (FORCE)')


@pytest.fixture(scope="module")
def server_url() -> str:
    url = os.environ.get("TEST_DATABASE_URL")
    if url is None:
        pytest.skip("set TEST_DATABASE_URL to run PostgreSQL integration tests")
    return url


@pytest.fixture(scope="module")
def tiny_run(
    profile: ModuleType, server_url: str, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[TinyRun]:
    """One end-to-end run on a tiny volume, its database kept for the assertions."""
    explain = tmp_path_factory.mktemp("dashboard_profile") / "plans.md"
    finished = _run_profile(profile, server_url, "--keep", "--explain-out", str(explain))
    try:
        yield TinyRun(
            finished.code, finished.stdout, make_url(server_url), finished.database, explain
        )
    finally:
        _drop_database(server_url, finished.database)


@pytest.fixture(scope="module")
def unkept_run(profile: ModuleType, server_url: str) -> Finished:
    """The same seed again, without `--keep`."""
    return _run_profile(profile, server_url)


@pytest.fixture(scope="module")
def failed_run(profile: ModuleType, server_url: str) -> Finished:
    """A volume too small to fill a page, without `--keep`."""
    return _run_profile(profile, server_url, "--prs-per-day", "1", "--history-days", "1")


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
def test_the_same_seed_gives_the_same_rows(tiny_run: TinyRun, unkept_run: Finished) -> None:
    assert unkept_run.code == 0, unkept_run.stdout
    digest = re.compile(r"rows digest: ([0-9a-f]{32})")
    first, second = digest.search(tiny_run.stdout), digest.search(unkept_run.stdout)
    assert first is not None and second is not None
    assert first.group(1) == second.group(1)


@pytest.mark.integration
@pytest.mark.parametrize("finished", ["unkept_run", "failed_run"])
def test_a_database_without_keep_is_dropped_also_when_the_measurement_fails(
    request: pytest.FixtureRequest, server_url: str, finished: str
) -> None:
    run: Finished = request.getfixturevalue(finished)
    assert not _database_exists(server_url, run.database)


@pytest.mark.integration
@pytest.mark.parametrize(
    "scenario", ["summary 30d", "summary 30d repo", "runs first page", "runs by cursor"]
)
def test_the_command_prints_p50_and_p95_of_every_scenario(tiny_run: TinyRun, scenario: str) -> None:
    assert tiny_run.code == 0, tiny_run.stdout
    number = r"\d+\.\d{2}"
    line = rf"^{scenario}\s+3\s+(?P<p50>{number})\s+(?P<p95>{number})\s+{number}\s+{number}$"
    match = re.search(line, tiny_run.stdout, re.MULTILINE)
    assert match is not None, tiny_run.stdout
    assert 0 < float(match["p50"]) <= float(match["p95"])


@pytest.mark.integration
def test_a_volume_without_a_full_page_fails_the_measurement(failed_run: Finished) -> None:
    """A profile of nearly empty data would lie; the run says so instead of printing numbers."""
    assert failed_run.code == 1
    assert "full page" in failed_run.stderr


@pytest.mark.integration
def test_the_plans_are_of_the_real_statements_with_buffers(tiny_run: TinyRun) -> None:
    assert tiny_run.code == 0, tiny_run.stdout
    plans = tiny_run.explain.read_text()
    for scenario in ("summary 30d", "summary 30d repo", "runs first page", "runs by cursor"):
        assert f"## {scenario}\n" in plans
    assert "FROM runs" in plans
    assert "actual time=" in plans
    assert "Buffers: shared" in plans


@pytest.mark.integration
def test_the_list_runs_one_statement_at_any_page_size(tiny_run: TinyRun) -> None:
    """The baseline of the Part 2 N+1 criterion: statements per request at two page sizes."""
    counts = [
        re.search(rf"^runs limit={limit}\s+(\d+)\s", tiny_run.stdout, re.MULTILINE)
        for limit in (10, 100)
    ]
    assert all(counts), tiny_run.stdout
    assert [int(match.group(1)) for match in counts if match] == [1, 1]


@pytest.mark.integration
def test_the_share_of_saved_results_is_printed(tiny_run: TinyRun) -> None:
    assert 0 < tiny_run.printed("with_saved_result") <= tiny_run.printed("window_succeeded")


@pytest.mark.integration
def test_the_plans_stay_readable_and_list_every_statement(tiny_run: TinyRun) -> None:
    """A bound list of run ids runs to tens of kilobytes per line; the report has to hold it."""
    plans = tiny_run.explain.read_text()
    assert max(len(line) for line in plans.splitlines()) <= 500
    assert "chars cut]" in plans
    section = plans.split("## summary 30d\n", 1)[1].split("\n## ", 1)[0]
    assert len(re.findall(r"^\| \d+ \| \d+\.\d{2} \|", section, re.MULTILINE)) >= 5


@pytest.mark.integration
def test_the_machine_load_is_printed_on_both_sides_of_the_timing(tiny_run: TinyRun) -> None:
    assert re.search(r"^environment: .* jit=\w+ .*load=[\d. ]+$", tiny_run.stdout, re.MULTILINE)
    assert re.search(r"^load after timing: [\d. ]+$", tiny_run.stdout, re.MULTILINE)


@pytest.mark.parametrize("variable", ["PGHOSTADDR", "PGSERVICE"])
def test_a_libpq_variable_that_redirects_the_connection_is_refused(
    profile: ModuleType,
    variable: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """libpq reads these when the URL names no hostaddr or service, past the host checked."""
    monkeypatch.setenv(variable, "192.0.2.1")
    assert profile.main(["--server-url", _LOOPBACK]) == 2
    error = capsys.readouterr().err
    assert variable in error
    assert "loopback" in error


@pytest.mark.parametrize("host", ["localhost", "127.0.0.1", "127.0.0.2", "[::1]"])
def test_a_loopback_server_passes_the_guard(profile: ModuleType, host: str) -> None:
    url = profile.loopback_server_url(f"postgresql+psycopg://app:app@{host}:5432/app")
    assert url.database == "app"
