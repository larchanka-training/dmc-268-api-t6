import asyncio
from datetime import UTC, datetime
from uuid import UUID

from app.modules.reviews.application.list_runs import ListRuns, RunCursor, RunListItem


class FakeRunRepository:
    def __init__(self) -> None:
        self.args: tuple[str | None, str | None, RunCursor | None, int] | None = None

    async def list_runs(
        self,
        *,
        status: str | None,
        repository: str | None,
        cursor: RunCursor | None,
        limit: int,
    ) -> list[RunListItem]:
        self.args = status, repository, cursor, limit
        return []


def test_list_runs_forwards_optional_filters_to_the_repository() -> None:
    repository = FakeRunRepository()

    result = asyncio.run(ListRuns(repository).execute(status="succeeded", repository="org/repo"))

    assert result.items == []
    assert result.next_cursor is None
    assert repository.args == ("succeeded", "org/repo", None, 51)


def test_list_runs_builds_a_cursor_from_the_last_returned_item() -> None:
    created_at = datetime(2026, 9, 24, tzinfo=UTC)
    first = RunListItem(
        id=UUID("00000000-0000-0000-0000-000000000001"),
        status="succeeded",
        engine="fast",
        attempt=0,
        cancel_requested=False,
        started_at=None,
        finished_at=None,
        error_code=None,
        model=None,
        action_count=0,
        repo="org/repo",
        number=1,
        title="First",
        url="https://example.test/1",
        head_sha="a" * 40,
        created_at=created_at,
    )
    second = RunListItem(
        id=UUID("00000000-0000-0000-0000-000000000002"),
        status="queued",
        engine="deep",
        attempt=1,
        cancel_requested=False,
        started_at=None,
        finished_at=None,
        error_code=None,
        model=None,
        action_count=0,
        repo="org/repo",
        number=2,
        title="Second",
        url="https://example.test/2",
        head_sha="b" * 40,
        created_at=created_at,
    )

    class FullPageRepository(FakeRunRepository):
        async def list_runs(
            self,
            *,
            status: str | None,
            repository: str | None,
            cursor: RunCursor | None,
            limit: int,
        ) -> list[RunListItem]:
            return [first, second]

    result = asyncio.run(ListRuns(FullPageRepository()).execute(limit=1))

    assert result.items == [first]
    assert result.next_cursor is not None
    assert RunCursor.decode(result.next_cursor) == RunCursor(created_at=created_at, id=first.id)
