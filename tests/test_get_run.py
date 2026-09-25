import asyncio
from datetime import UTC, datetime
from uuid import UUID

from app.modules.reviews.application.get_run import GetRun
from app.modules.reviews.application.list_runs import RunListItem


class FakeRunDetailRepository:
    def __init__(self, item: RunListItem | None) -> None:
        self.item = item
        self.requested_id: UUID | None = None

    async def get_run(self, run_id: UUID) -> RunListItem | None:
        self.requested_id = run_id
        return self.item


def test_get_run_returns_the_repository_projection() -> None:
    run_id = UUID("00000000-0000-0000-0000-000000000001")
    item = RunListItem(
        id=run_id,
        status="succeeded",
        engine="fast",
        attempt=0,
        cancel_requested=False,
        started_at=datetime(2026, 9, 24, tzinfo=UTC),
        finished_at=None,
        error_code=None,
        model=None,
        action_count=0,
        repo="org/repo",
        number=1,
        title="Title",
        url="https://example.test/1",
        head_sha="a" * 40,
        created_at=datetime(2026, 9, 24, tzinfo=UTC),
    )
    repository = FakeRunDetailRepository(item)

    result = asyncio.run(GetRun(repository).execute(run_id))

    assert result == item
    assert repository.requested_id == run_id
