"""Admission at the host-supplied rollback checker's public entry point."""

from __future__ import annotations

import logging
import os
import runpy
import subprocess
from collections.abc import Callable
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any, cast

import pytest
from alembic.config import Config
from pytest import CaptureFixture, LogCaptureFixture
from sqlalchemy.pool import NullPool

CHECKER = Path(__file__).resolve().parents[1] / "deploy/scripts/check-rollback-revision.py"


class Graph:
    def __init__(
        self,
        target_heads: tuple[str, ...] = ("20261010_0002",),
        known: set[str] | None = None,
    ) -> None:
        self.target_heads = target_heads
        self.known = known if known is not None else {"20261010_0001", "20261010_0002"}

    def heads(self) -> tuple[str, ...]:
        return self.target_heads

    def revisions(self) -> set[str]:
        return self.known

    def ancestors(self, head: str) -> set[str]:
        return {"20261010_0001", "20261010_0002"}


def checker_main() -> Callable[..., int]:
    return cast(Callable[..., int], runpy.run_path(str(CHECKER))["main"])


def test_current_target_head_is_admitted(capsys: CaptureFixture[str]) -> None:
    main = checker_main()
    assert main(graph_factory=Graph, revision_reader=lambda: ("20261010_0002",)) == 0
    assert capsys.readouterr().out == "rollback revision check: compatible\n"


def test_recognized_ancestor_is_admitted(capsys: CaptureFixture[str]) -> None:
    assert checker_main()(graph_factory=Graph, revision_reader=lambda: ("20261010_0001",)) == 0
    assert capsys.readouterr().out == "rollback revision check: compatible\n"


@pytest.mark.parametrize(
    ("heads", "current", "known", "reason"),
    [
        (("20261010_0002",), ("20261010_0099",), None, "database revision is unknown"),
        (
            ("20261010_0002",),
            ("20261010_0009",),
            {"20261010_0001", "20261010_0002", "20261010_0009"},
            "database revision is not an ancestor of target head",
        ),
        ((), ("20261010_0002",), None, "target must have exactly one head"),
        (
            ("20261010_0002", "20261010_0003"),
            ("20261010_0002",),
            None,
            "target must have exactly one head",
        ),
        (("20261010_0002",), (), None, "database must have exactly one tracked revision"),
        (
            ("20261010_0002",),
            ("20261010_0001", "20261010_0002"),
            None,
            "database must have exactly one tracked revision",
        ),
    ],
)
def test_unsupported_revision_state_is_refused(
    heads: tuple[str, ...],
    current: tuple[str, ...],
    known: set[str] | None,
    reason: str,
    capsys: CaptureFixture[str],
) -> None:
    assert (
        checker_main()(graph_factory=lambda: Graph(heads, known), revision_reader=lambda: current)
        == 1
    )
    output = capsys.readouterr()
    assert output.out == ""
    assert output.err == f"rollback revision check: refused: {reason}\n"


@pytest.mark.parametrize("failure", ["factory", "heads", "revisions", "ancestors", "query"])
def test_probe_errors_fail_closed_without_secrets(
    failure: str, capsys: CaptureFixture[str], caplog: LogCaptureFixture
) -> None:
    secret = "postgresql://app:secret-canary@db/private\nPEM-BODY-CANARY"

    def fail() -> None:
        print(secret)
        logging.getLogger("rollback-driver").warning(secret)
        raise RuntimeError(secret)

    class BrokenGraph(Graph):
        def heads(self) -> tuple[str, ...]:
            if failure == "heads":
                fail()
            return super().heads()

        def revisions(self) -> set[str]:
            if failure == "revisions":
                fail()
            return super().revisions()

        def ancestors(self, head: str) -> set[str]:
            if failure == "ancestors":
                fail()
            return super().ancestors(head)

    def graph_factory() -> BrokenGraph:
        if failure == "factory":
            fail()
        return BrokenGraph()

    def reader() -> tuple[str, ...]:
        if failure == "query":
            fail()
        return ("20261010_0002",)

    assert checker_main()(graph_factory=graph_factory, revision_reader=reader) == 1
    output = capsys.readouterr()
    assert output.out == ""
    assert (
        output.err == "rollback revision check: refused: cannot inspect target graph or database\n"
    )
    assert caplog.text == ""


class Connection(AbstractContextManager["Connection"]):
    def __init__(self) -> None:
        self.closed = False

    def __enter__(self) -> Connection:
        return self

    def __exit__(self, *args: object) -> None:
        self.closed = True


@pytest.mark.parametrize("failure", [None, "connect", "query", "missing", "empty"])
def test_runtime_database_probe_is_read_only_bounded_and_closes(
    failure: str | None, monkeypatch: pytest.MonkeyPatch, capsys: CaptureFixture[str]
) -> None:
    connection = Connection()
    options: dict[str, Any] = {}
    disposed: list[bool] = []
    url = "postgresql+psycopg://app:DB-SECRET-CANARY@db/app"
    monkeypatch.setenv("DATABASE_URL", url)

    class Engine:
        def connect(self) -> Connection:
            if failure == "connect":
                raise RuntimeError(url)
            return connection

        def dispose(self) -> None:
            disposed.append(True)

    def create_engine(actual_url: str, **kwargs: Any) -> Engine:
        assert actual_url == url
        options.update(kwargs)
        return Engine()

    class Context:
        def get_current_heads(self) -> tuple[str, ...]:
            if failure == "query":
                raise RuntimeError(url)
            # Alembic's public get_current_heads returns () for both absent and empty tables.
            if failure in {"missing", "empty"}:
                return ()
            return ("20261010_0002",)

    def configure(actual_connection: Connection) -> Context:
        assert actual_connection is connection
        return Context()

    monkeypatch.setattr("sqlalchemy.create_engine", create_engine)
    monkeypatch.setattr("alembic.runtime.migration.MigrationContext.configure", configure)
    assert checker_main()(graph_factory=Graph) == (0 if failure is None else 1)
    assert options == {
        "poolclass": NullPool,
        "connect_args": {
            "connect_timeout": 5,
            "options": (
                "-c default_transaction_read_only=on -c statement_timeout=5000 "
                "-c lock_timeout=5000 -c idle_in_transaction_session_timeout=10000"
            ),
        },
    }
    assert disposed == [True]
    assert connection.closed is (failure != "connect")
    output = capsys.readouterr()
    assert "DB-SECRET-CANARY" not in output.out + output.err


@pytest.mark.parametrize(
    ("migrations", "current", "expected"),
    [
        ([("20261010_0001", None), ("20261010_0002", "20261010_0001")], "20261010_0002", 0),
        ([("20261010_0001", None), ("20261010_0002", "20261010_0001")], "20261010_0001", 0),
        ([("20261010_0001", None), ("20261010_0002", "20261010_0001")], "20261010_0099", 1),
        ([("20261010_0001", None), ("20261010_0002", None)], "20261010_0001", 1),
        ([("20261010_0002", "MISSING-SECRET-CANARY")], "20261010_0002", 1),
        (
            [("20261010_0001", "20261010_0002"), ("20261010_0002", "20261010_0001")],
            "20261010_0002",
            1,
        ),
        ([("20261010_0001", None), ("20261010_0001", None)], "20261010_0001", 1),
        ([], "20261010_0001", 1),
    ],
)
def test_target_graph_uses_image_migrations_without_running_bootstrap(
    migrations: list[tuple[str, str | None]],
    current: str,
    expected: int,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: CaptureFixture[str],
) -> None:
    versions = tmp_path / "alembic/versions"
    versions.mkdir(parents=True)
    for index, (revision, parent) in enumerate(migrations):
        (versions / f"migration_{index}.py").write_text(
            f"revision = {revision!r}\ndown_revision = {parent!r}\n"
            "def upgrade():\n    raise AssertionError('must not migrate')\n"
            "def downgrade():\n    raise AssertionError('must not downgrade')\n"
        )
    (tmp_path / "alembic/env.py").write_text("raise AssertionError('must not run env.py')\n")
    config = Config()
    config.set_main_option("script_location", str(tmp_path / "alembic"))

    def image_config(path: str) -> Config:
        assert path == "/srv/alembic.ini"
        return config

    monkeypatch.setattr("alembic.config.Config", image_config)
    assert checker_main()(revision_reader=lambda: (current,)) == expected
    output = capsys.readouterr()
    assert "MISSING-SECRET-CANARY" not in output.out + output.err
    assert "Traceback" not in output.out + output.err


@pytest.mark.parametrize("database_url", [None, "", "SECRET-CANARY", "sqlite:///secret-canary"])
def test_invalid_database_configuration_fails_closed(
    database_url: str | None, monkeypatch: pytest.MonkeyPatch, capsys: CaptureFixture[str]
) -> None:
    if database_url is None:
        monkeypatch.delenv("DATABASE_URL", raising=False)
    else:
        monkeypatch.setenv("DATABASE_URL", database_url)
    assert checker_main()(graph_factory=Graph) == 1
    output = capsys.readouterr()
    assert output.out == ""
    assert (
        output.err == "rollback revision check: refused: cannot inspect target graph or database\n"
    )


def test_host_source_on_stdin_returns_safe_nonzero_for_unusable_target() -> None:
    environment = dict(os.environ, DATABASE_URL="SECRET-CANARY")
    result = subprocess.run(
        ["uv", "run", "python", "-"],
        input=CHECKER.read_text(),
        text=True,
        capture_output=True,
        env=environment,
        check=False,
        timeout=20,
    )
    assert result.returncode == 1
    assert result.stdout == ""
    assert (
        result.stderr
        == "rollback revision check: refused: cannot inspect target graph or database\n"
    )
