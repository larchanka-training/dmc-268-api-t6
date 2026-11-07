"""The background re-read of a repository's visibility, over fakes (#454)."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from types import TracebackType
from typing import Self, cast

import pytest

from app.modules.repositories.application.repository_visibility import (
    RefreshRepositoryVisibility,
    RepositoryVisibilityDue,
    RepositoryVisibilityReading,
    RepositoryVisibilityTarget,
    RepositoryVisibilityUnavailable,
    RepositoryVisibilityUnitOfWork,
)

T0 = datetime(2026, 11, 1, 12, tzinfo=UTC)
PUBLIC = RepositoryVisibilityReading(False, "octo/a", "https://github.test/octo/a")


@dataclass
class _Store:
    targets: tuple[RepositoryVisibilityTarget, ...]
    due_calls: list[tuple[timedelta, int]] = field(default_factory=list)
    overwritten: list[tuple[int, int, bool | None, datetime]] = field(default_factory=list)
    renamed: list[tuple[int, int, str, str | None, datetime]] = field(default_factory=list)
    failed: list[tuple[int, int, datetime]] = field(default_factory=list)

    async def due(self, *, older_than: timedelta, limit: int) -> RepositoryVisibilityDue:
        self.due_calls.append((older_than, limit))
        return RepositoryVisibilityDue(T0, self.targets)

    async def overwrite(
        self,
        installation_external_id: int,
        repository_external_id: int,
        private: bool | None,
        observed_at: datetime,
    ) -> bool:
        self.overwritten.append(
            (installation_external_id, repository_external_id, private, observed_at)
        )
        return True

    async def rename(
        self,
        installation_external_id: int,
        repository_external_id: int,
        full_name: str,
        web_url: str | None,
        observed_at: datetime,
    ) -> bool:
        self.renamed.append(
            (installation_external_id, repository_external_id, full_name, web_url, observed_at)
        )
        return True

    async def record_failure(
        self, installation_external_id: int, repository_external_id: int, failed_at: datetime
    ) -> None:
        self.failed.append((installation_external_id, repository_external_id, failed_at))


@dataclass
class _Database:
    store: _Store
    active: bool = False
    commits: int = 0


@dataclass
class _Uow:
    database: _Database

    @property
    def visibility(self) -> _Store:
        return self.database.store

    async def __aenter__(self) -> Self:
        self.database.active = True
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.database.active = False

    async def commit(self) -> None:
        self.database.commits += 1

    async def rollback(self) -> None:
        raise AssertionError("nothing rolls back here")


@dataclass
class _Reader:
    database: _Database
    answers: dict[int, RepositoryVisibilityReading | None | Exception]
    reads: list[tuple[int, int]] = field(default_factory=list)

    async def read(
        self, installation_external_id: int, repository_external_id: int
    ) -> RepositoryVisibilityReading | None:
        assert not self.database.active, "a GitHub call must not sit inside a transaction"
        self.reads.append((installation_external_id, repository_external_id))
        answer = self.answers[repository_external_id]
        if isinstance(answer, Exception):
            raise answer
        return answer


def _run(
    answers: dict[int, RepositoryVisibilityReading | None | Exception],
) -> tuple[int, _Store, _Reader, _Database]:
    store = _Store(tuple(RepositoryVisibilityTarget(17, repo) for repo in answers))
    database = _Database(store)
    reader = _Reader(database, answers)
    refreshed = asyncio.run(
        RefreshRepositoryVisibility(
            uow_factory=lambda: cast(RepositoryVisibilityUnitOfWork, _Uow(database)),
            reader=reader,
        ).execute()
    )
    return refreshed, store, reader, database


def test_the_oldest_rows_of_one_batch_are_asked_for() -> None:
    _, store, _, _ = _run({})

    assert store.due_calls == [(timedelta(hours=24), 50)]


@pytest.mark.parametrize(
    ("reading", "written", "renamed"),
    [
        pytest.param(PUBLIC, False, ("octo/a", "https://github.test/octo/a"), id="public"),
        pytest.param(
            RepositoryVisibilityReading(True, "octo/a", None), True, ("octo/a", None), id="private"
        ),
        pytest.param(
            RepositoryVisibilityReading(None, "octo/a", None), None, ("octo/a", None), id="no-flag"
        ),
        pytest.param(RepositoryVisibilityReading(False, None, None), False, None, id="bad-name"),
        pytest.param(None, None, None, id="github-does-not-show-it"),
    ],
)
def test_a_read_is_written_with_the_time_taken_before_the_batch(
    reading: RepositoryVisibilityReading | None,
    written: bool | None,
    renamed: tuple[str, str | None] | None,
) -> None:
    refreshed, store, _, database = _run({101: reading})

    assert refreshed == 1
    assert store.overwritten == [(17, 101, written, T0)]
    assert store.renamed == ([] if renamed is None else [(17, 101, *renamed, T0)])
    assert database.commits == 1


def test_a_batch_stops_after_three_failed_reads() -> None:
    failure = RepositoryVisibilityUnavailable("GitHub is away")

    refreshed, store, reader, _ = _run(dict.fromkeys(range(101, 106), failure))

    assert refreshed == 0
    assert len(reader.reads) == 3
    assert store.overwritten == []
    assert store.failed == [(17, 101, T0), (17, 102, T0), (17, 103, T0)]


def test_a_failed_read_moves_its_row_back_and_the_next_one_still_runs(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING):
        refreshed, store, _, database = _run(
            {
                101: RepositoryVisibilityUnavailable("GitHub is away"),
                102: RuntimeError("a bug in a read"),
                103: PUBLIC,
            }
        )

    assert refreshed == 1
    assert [row[1] for row in store.overwritten] == [103]
    # Only the failure is written for these rows, so the next pass reads the others first.
    assert store.failed == [(17, 101, T0), (17, 102, T0)]
    assert database.commits == 3
    assert "repository 101" in caplog.text
    assert "repository 102" in caplog.text
