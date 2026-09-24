import asyncio

from app.modules.reviews.application.list_runs import ListRuns, RunListItem


class FakeRunRepository:
    def __init__(self) -> None:
        self.args: tuple[str | None, str | None, int] | None = None

    async def list_runs(
        self, *, status: str | None, repository: str | None, limit: int
    ) -> list[RunListItem]:
        self.args = status, repository, limit
        return []


def test_list_runs_forwards_optional_filters_to_the_repository() -> None:
    repository = FakeRunRepository()

    result = asyncio.run(ListRuns(repository).execute(status="succeeded", repository="org/repo"))

    assert result == []
    assert repository.args == ("succeeded", "org/repo", 50)
