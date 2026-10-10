from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

import pytest

from app.main import app, get_run_repository
from app.modules.reviews.application.get_run import RunReview, RunReviewRepository
from app.modules.reviews.application.list_runs import RunCursor, RunListItem
from tests.portal_test_client import authenticated_test_client as TestClient


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


class FakeRunDetailRepository:
    def __init__(self, item: RunListItem | None) -> None:
        self.item = item
        self.calls: list[UUID] = []

    async def get_run(self, run_id: UUID) -> RunListItem | None:
        self.calls.append(run_id)
        return self.item

    async def get_run_review(self, run_id: UUID) -> RunReview | None:
        return empty_review() if self.item is not None else None


def empty_review() -> RunReview:
    return RunReview(
        author=None,
        head_ref=None,
        base_ref=None,
        findings=[],
        summary=None,
        usage_calls=0,
        tokens_in=0,
        tokens_out=0,
        cost_usd=Decimal("0"),
    )


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
        trigger="webhook",
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
                "trigger": "webhook",
                "createdAt": "2026-09-24T00:00:00Z",
                "startedAt": "2026-09-24T00:00:00Z",
                "finishedAt": None,
                "errorCode": None,
                "model": "gpt-test",
                "actionCount": 2,
                "summaryOnly": False,
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


def test_runs_list_accepts_a_repo_filter_of_512_characters_and_rejects_one_of_513() -> None:
    longest = "o/" + "r" * 510
    too_long = "o/" + "r" * 511
    repository = FakeRunRepository([])
    app.dependency_overrides[get_run_repository] = lambda: repository
    try:
        client = TestClient(app)
        accepted = client.get("/api/runs", params={"repo": longest})
        rejected = client.get("/api/runs", params={"repo": too_long})
    finally:
        app.dependency_overrides.clear()

    assert accepted.status_code == 200
    assert rejected.status_code == 422
    assert repository.calls == [(None, longest, None, 51)]


def test_run_detail_returns_pr_data_latest_model_and_action_count() -> None:
    item = replace(make_item(7, datetime(2026, 9, 24, tzinfo=UTC)), summary_only=True)
    repository: RunReviewRepository = FakeRunDetailRepository(item)
    app.dependency_overrides[get_run_repository] = lambda: repository
    try:
        response = TestClient(app).get(f"/api/runs/{item.id}")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json()["pullRequest"] == {
        "repo": "org/repo",
        "number": 7,
        "title": "PR 7",
        "url": "https://example.test/7",
        "headSha": "a" * 40,
        "author": None,
        "headRef": None,
        "baseRef": None,
    }
    assert response.json()["model"] == "gpt-test"
    assert response.json()["actionCount"] == 2
    assert response.json()["summaryOnly"] is True


@pytest.mark.parametrize("trigger", ["webhook", "rerun"])
def test_runs_list_and_detail_return_the_stored_trigger_and_creation_time(trigger: str) -> None:
    # Queued and never claimed: createdAt is the only timestamp the Run has.
    item = replace(
        make_item(9, datetime(2026, 9, 24, 10, 30, 15, 250000, tzinfo=UTC)),
        status="queued",
        started_at=None,
        trigger=trigger,
    )
    try:
        client = TestClient(app)
        app.dependency_overrides[get_run_repository] = lambda: FakeRunRepository([item])
        listed = client.get("/api/runs")
        app.dependency_overrides[get_run_repository] = lambda: FakeRunDetailRepository(item)
        detail = client.get(f"/api/runs/{item.id}")
    finally:
        app.dependency_overrides.clear()

    assert listed.status_code == 200
    assert detail.status_code == 200
    for run in (listed.json()["items"][0], detail.json()):
        assert run["trigger"] == trigger
        assert run["createdAt"] == "2026-09-24T10:30:15.250000Z"
        assert run["startedAt"] is None


def test_run_detail_returns_null_model_when_the_run_has_no_usage_event() -> None:
    item = replace(make_item(8, datetime(2026, 9, 24, tzinfo=UTC)), model=None, action_count=3)
    repository: RunReviewRepository = FakeRunDetailRepository(item)
    app.dependency_overrides[get_run_repository] = lambda: repository
    try:
        response = TestClient(app).get(f"/api/runs/{item.id}")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json()["model"] is None
    assert response.json()["actionCount"] == 3


def test_run_detail_returns_404_for_a_missing_run_and_422_for_invalid_id() -> None:
    repository: RunReviewRepository = FakeRunDetailRepository(None)
    app.dependency_overrides[get_run_repository] = lambda: repository
    try:
        client = TestClient(app)
        missing = client.get("/api/runs/00000000-0000-0000-0000-000000000999")
        invalid = client.get("/api/runs/not-a-uuid")
    finally:
        app.dependency_overrides.clear()

    assert missing.status_code == 404
    assert invalid.status_code == 422
