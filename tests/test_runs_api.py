from datetime import UTC, datetime
from uuid import UUID

from fastapi.testclient import TestClient

from app.main import app, get_run_repository
from app.modules.reviews.application.list_runs import RunCursor, RunListItem


class FakeRunRepository:
    def __init__(self, items: list[RunListItem]) -> None:
        self.items = items
        self.calls: list[tuple[str | None, str | None, RunCursor | None, int]] = []

    async def list_runs(
        self,
        *,
        status: str | None,
        repository: str | None,
        cursor: RunCursor | None,
        limit: int,
    ) -> list[RunListItem]:
        self.calls.append((status, repository, cursor, limit))
        return self.items


def make_item(value: int, created_at: datetime) -> RunListItem:
    return RunListItem(
        id=UUID(f"00000000-0000-0000-0000-{value:012d}"),
        status="succeeded",
        engine="fast",
        attempt=0,
        cancel_requested=False,
        started_at=created_at,
        finished_at=None,
        error_code=None,
        model="gpt-test",
        action_count=2,
        repo="org/repo",
        number=value,
        title=f"PR {value}",
        url=f"https://example.test/{value}",
        head_sha="a" * 40,
        created_at=created_at,
    )


def test_runs_list_uses_the_camel_case_contract() -> None:
    item = make_item(1, datetime(2026, 9, 24, tzinfo=UTC))
    repository = FakeRunRepository([item])
    app.dependency_overrides[get_run_repository] = lambda: repository
    try:
        response = TestClient(app).get("/api/runs")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json() == {
        "items": [
            {
                "id": str(item.id),
                "status": "succeeded",
                "engine": "fast",
                "attempt": 0,
                "cancelRequested": False,
                "startedAt": "2026-09-24T00:00:00Z",
                "finishedAt": None,
                "errorCode": None,
                "model": "gpt-test",
                "actionCount": 2,
                "pullRequest": {
                    "repo": "org/repo",
                    "number": 1,
                    "title": "PR 1",
                    "url": "https://example.test/1",
                    "headSha": "a" * 40,
                },
            }
        ],
        "nextCursor": None,
    }


def test_runs_list_validates_filters_and_passes_a_stable_cursor() -> None:
    created_at = datetime(2026, 9, 24, tzinfo=UTC)
    newest = make_item(3, created_at)
    middle = make_item(2, created_at)
    oldest = make_item(1, created_at)

    class PagingRunRepository(FakeRunRepository):
        async def list_runs(
            self,
            *,
            status: str | None,
            repository: str | None,
            cursor: RunCursor | None,
            limit: int,
        ) -> list[RunListItem]:
            self.calls.append((status, repository, cursor, limit))
            items = self.items
            if cursor is not None:
                items = [
                    item
                    for item in items
                    if (item.created_at, item.id) < (cursor.created_at, cursor.id)
                ]
            return items[:limit]

    repository = PagingRunRepository([newest, middle, oldest])
    app.dependency_overrides[get_run_repository] = lambda: repository
    try:
        response = TestClient(app).get(
            "/api/runs", params={"status": "succeeded", "repo": "org/repo", "limit": 2}
        )
        second_page = TestClient(app).get(
            "/api/runs", params={"cursor": response.json()["nextCursor"], "limit": 2}
        )
        invalid_status = TestClient(app).get("/api/runs", params={"status": "unknown"})
        invalid_limit = TestClient(app).get("/api/runs", params={"limit": 101})
        invalid_cursor = TestClient(app).get("/api/runs", params={"cursor": "not-a-cursor"})
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert [item["id"] for item in response.json()["items"]] == [str(newest.id), str(middle.id)]
    assert response.json()["nextCursor"] is not None
    assert [item["id"] for item in second_page.json()["items"]] == [str(oldest.id)]
    assert len({item["id"] for item in response.json()["items"] + second_page.json()["items"]}) == 3
    assert repository.calls == [
        ("succeeded", "org/repo", None, 3),
        (None, None, RunCursor(created_at=created_at, id=middle.id), 3),
    ]
    assert invalid_status.status_code == 422
    assert invalid_limit.status_code == 422
    assert invalid_cursor.status_code == 422


def test_runs_list_rejects_malformed_base64_and_non_utf8_cursors() -> None:
    repository = FakeRunRepository([])
    app.dependency_overrides[get_run_repository] = lambda: repository
    try:
        client = TestClient(app, raise_server_exceptions=False)
        malformed_base64 = client.get("/api/runs", params={"cursor": "A"})
        non_utf8 = client.get("/api/runs", params={"cursor": "_w"})
        non_ascii = client.get("/api/runs", params={"cursor": "é"})
    finally:
        app.dependency_overrides.clear()

    assert malformed_base64.status_code == 422
    assert non_utf8.status_code == 422
    assert non_ascii.status_code == 422
