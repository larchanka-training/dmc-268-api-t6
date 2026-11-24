"""SQLite state of the bot: pull request snapshots, tracked requests and a few markers.

Every write is its own short transaction; no network call runs inside one. The "notified" flag
of a request is the only record of what was sent, so a restart never sends it again.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from datetime import datetime

from review_bot.models import PullRequest, Request, Status

SCHEMA = """
CREATE TABLE IF NOT EXISTS prs (
    repo TEXT NOT NULL,
    number INTEGER NOT NULL,
    title TEXT NOT NULL,
    author TEXT NOT NULL,
    url TEXT NOT NULL,
    draft INTEGER NOT NULL,
    open INTEGER NOT NULL,
    labels TEXT NOT NULL,
    requested TEXT NOT NULL,
    assign_done INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (repo, number)
);
CREATE TABLE IF NOT EXISTS requests (
    repo TEXT NOT NULL,
    number INTEGER NOT NULL,
    reviewer TEXT NOT NULL,
    login TEXT NOT NULL,
    round INTEGER NOT NULL,
    status TEXT NOT NULL,
    queued_at TEXT NOT NULL,
    notified INTEGER NOT NULL,
    reviews_seen INTEGER NOT NULL,
    PRIMARY KEY (repo, number, reviewer)
);
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

Key = tuple[str, int, str]


class Store:
    def __init__(self, path: str) -> None:
        self._db = sqlite3.connect(path, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(SCHEMA)

    def close(self) -> None:
        self._db.close()

    @contextmanager
    def transaction(self) -> Iterator[None]:
        self._db.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self._db.execute("ROLLBACK")
            raise
        self._db.execute("COMMIT")

    # meta

    def get_meta(self, key: str) -> str | None:
        row = self._db.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return None if row is None else str(row["value"])

    def set_meta(self, key: str, value: str) -> None:
        self._db.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) "
            "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
            (key, value),
        )

    # pull requests

    def save_open_prs(self, repo: str, prs: Iterable[PullRequest]) -> None:
        """Upsert the open pull requests of a repo and mark every other one of it closed."""
        prs = list(prs)
        self._db.execute("UPDATE prs SET open = 0 WHERE repo = ?", (repo,))
        for pr in prs:
            self._db.execute(
                "INSERT INTO prs (repo, number, title, author, url, draft, open, labels, requested)"
                " VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?) ON CONFLICT (repo, number) DO UPDATE SET"
                " title = excluded.title, author = excluded.author, url = excluded.url,"
                " draft = excluded.draft, open = 1, labels = excluded.labels,"
                " requested = excluded.requested",
                (
                    pr.repo,
                    pr.number,
                    pr.title,
                    pr.author,
                    pr.url,
                    int(pr.draft),
                    json.dumps(sorted(pr.labels)),
                    json.dumps(list(pr.requested_reviewers)),
                ),
            )

    def ready_prs(self) -> dict[tuple[str, int], PullRequest]:
        """Open, non-draft pull requests of the last successful poll."""
        rows = self._db.execute("SELECT * FROM prs WHERE open = 1 AND draft = 0").fetchall()
        return {
            (row["repo"], row["number"]): PullRequest(
                repo=row["repo"],
                number=row["number"],
                title=row["title"],
                author=row["author"],
                url=row["url"],
                draft=False,
                labels=frozenset(json.loads(row["labels"])),
                requested_reviewers=tuple(json.loads(row["requested"])),
            )
            for row in rows
        }

    def assign_done(self, repo: str, number: int) -> bool:
        row = self._db.execute(
            "SELECT assign_done FROM prs WHERE repo = ? AND number = ?", (repo, number)
        ).fetchone()
        return row is not None and bool(row["assign_done"])

    def mark_assign_done(self, repo: str, number: int) -> None:
        self._db.execute(
            "UPDATE prs SET assign_done = 1 WHERE repo = ? AND number = ?", (repo, number)
        )

    # requests

    def requests_of(self, repo: str, number: int) -> dict[str, Request]:
        rows = self._db.execute(
            "SELECT * FROM requests WHERE repo = ? AND number = ?", (repo, number)
        ).fetchall()
        return {row["reviewer"]: _request(row) for row in rows}

    def put_request(self, request: Request) -> None:
        self._db.execute(
            "INSERT INTO requests (repo, number, reviewer, login, round, status, queued_at,"
            " notified, reviews_seen) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT (repo, number, reviewer) DO UPDATE SET login = excluded.login,"
            " round = excluded.round, status = excluded.status, queued_at = excluded.queued_at,"
            " notified = excluded.notified, reviews_seen = excluded.reviews_seen",
            (
                request.repo,
                request.number,
                request.reviewer,
                request.login,
                request.round,
                request.status.value,
                request.queued_at.isoformat(),
                int(request.notified),
                request.reviews_seen,
            ),
        )

    def waiting(self) -> list[Request]:
        """Requests in status waiting, of any pull request."""
        rows = self._db.execute("SELECT * FROM requests WHERE status = ?", (Status.WAITING.value,))
        return [_request(row) for row in rows.fetchall()]

    def set_notified(self, keys: Iterable[Key], notified: bool) -> None:
        for repo, number, reviewer in keys:
            self._db.execute(
                "UPDATE requests SET notified = ? WHERE repo = ? AND number = ? AND reviewer = ?",
                (int(notified), repo, number, reviewer),
            )


def _request(row: sqlite3.Row) -> Request:
    return Request(
        repo=row["repo"],
        number=row["number"],
        reviewer=row["reviewer"],
        login=row["login"],
        round=row["round"],
        status=Status(row["status"]),
        queued_at=datetime.fromisoformat(row["queued_at"]),
        notified=bool(row["notified"]),
        reviews_seen=row["reviews_seen"],
    )
