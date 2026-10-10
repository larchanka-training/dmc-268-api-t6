"""Read-only admission probe supplied on stdin to the target image's Python."""

from __future__ import annotations

import logging
import os
import sys
import warnings
from collections.abc import Callable
from contextlib import redirect_stderr, redirect_stdout
from typing import Protocol


class RevisionGraph(Protocol):
    def heads(self) -> tuple[str, ...]: ...

    def revisions(self) -> set[str]: ...

    def ancestors(self, head: str) -> set[str]: ...


class AlembicGraph:
    def __init__(self) -> None:
        from alembic.config import Config
        from alembic.script import ScriptDirectory

        self.directory = ScriptDirectory.from_config(Config("/srv/alembic.ini"))

    def heads(self) -> tuple[str, ...]:
        return tuple(self.directory.get_heads())

    def revisions(self) -> set[str]:
        return {revision.revision for revision in self.directory.walk_revisions()}

    def ancestors(self, head: str) -> set[str]:
        return {revision.revision for revision in self.directory.iterate_revisions(head, "base")}


def read_database_revisions() -> tuple[str, ...]:
    # Imports remain inside the controlled probe boundary for older target images.
    from alembic.runtime.migration import MigrationContext
    from sqlalchemy import create_engine
    from sqlalchemy.engine import make_url
    from sqlalchemy.pool import NullPool

    url = os.environ["DATABASE_URL"]
    if make_url(url).drivername != "postgresql+psycopg":
        raise ValueError("unsupported database driver")
    engine = create_engine(
        url,
        poolclass=NullPool,
        connect_args={
            "connect_timeout": 5,
            "options": (
                "-c default_transaction_read_only=on -c statement_timeout=5000 "
                "-c lock_timeout=5000 -c idle_in_transaction_session_timeout=10000"
            ),
        },
    )
    try:
        with engine.connect() as connection:
            return MigrationContext.configure(connection).get_current_heads()
    finally:
        engine.dispose()


def main(
    *,
    graph_factory: Callable[[], RevisionGraph] = AlembicGraph,
    revision_reader: Callable[[], tuple[str, ...]] = read_database_revisions,
) -> int:
    reason: str | None = None
    # Third-party graph/driver diagnostics can include connection strings or SQL parameters.
    # Report only controlled messages outside this boundary, never exception text.
    with (
        open(os.devnull, "w") as sink,
        redirect_stdout(sink),
        redirect_stderr(sink),
        warnings.catch_warnings(),
    ):
        previous_logging_disable = logging.root.manager.disable
        try:
            logging.disable(logging.CRITICAL)
            # Duplicate/missing revision warnings describe a malformed graph, never admission.
            warnings.simplefilter("error")
            graph = graph_factory()
            heads = graph.heads()
            if len(heads) != 1:
                reason = "target must have exactly one head"
            else:
                revisions = revision_reader()
                if len(revisions) != 1:
                    reason = "database must have exactly one tracked revision"
                elif revisions[0] not in graph.revisions():
                    reason = "database revision is unknown"
                elif revisions[0] not in graph.ancestors(heads[0]):
                    reason = "database revision is not an ancestor of target head"
        except Exception:
            reason = "cannot inspect target graph or database"
        finally:
            logging.disable(previous_logging_disable)
    if reason is None:
        print("rollback revision check: compatible")
        return 0
    print(f"rollback revision check: refused: {reason}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
