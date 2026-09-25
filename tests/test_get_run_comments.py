import asyncio
from datetime import UTC, datetime
from uuid import UUID

from fastapi.testclient import TestClient

from app.main import app, get_run_repository
from app.modules.reviews.application.get_run_comments import GetRunComments, PublishedComment


class FakeCommentsRepository:
    def __init__(self, comments: list[PublishedComment] | None) -> None:
        self.comments = comments
        self.calls: list[UUID] = []

    async def get_published_comments(self, run_id: UUID) -> list[PublishedComment] | None:
        self.calls.append(run_id)
        return self.comments


def make_comment(value: int, *, left: bool = False) -> PublishedComment:
    return PublishedComment(
        id=UUID(f"00000000-0000-0000-0000-{value:012d}"),
        file="app/service.py",
        old_line=17 if left else None,
        new_line=None if left else 23,
        end_line=19 if left else 25,
        severity="high",
        category="correctness",
        title="Incorrect state transition",
        body="This transition skips validation.",
        rule_name=None if left else "state-machine",
        created_at=datetime(2026, 9, 25, tzinfo=UTC),
    )


def test_get_run_comments_returns_the_repository_result() -> None:
    run_id = UUID("00000000-0000-0000-0000-000000000100")
    comment = make_comment(1)
    repository = FakeCommentsRepository([comment])

    result = asyncio.run(GetRunComments(repository).execute(run_id))

    assert result == [comment]
    assert repository.calls == [run_id]


def test_run_comments_match_the_ui_review_comment_contract() -> None:
    run_id = UUID("00000000-0000-0000-0000-000000000100")
    repository = FakeCommentsRepository([make_comment(1), make_comment(2, left=True)])
    app.dependency_overrides[get_run_repository] = lambda: repository
    try:
        response = TestClient(app).get(f"/api/runs/{run_id}/comments")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json() == [
        {
            "id": "00000000-0000-0000-0000-000000000001",
            "file": "app/service.py",
            "oldLine": None,
            "newLine": 23,
            "endLine": 25,
            "severity": "high",
            "category": "correctness",
            "title": "Incorrect state transition",
            "body": "This transition skips validation.",
            "ruleName": "state-machine",
            "createdAt": "2026-09-25T00:00:00Z",
        },
        {
            "id": "00000000-0000-0000-0000-000000000002",
            "file": "app/service.py",
            "oldLine": 17,
            "newLine": None,
            "endLine": 19,
            "severity": "high",
            "category": "correctness",
            "title": "Incorrect state transition",
            "body": "This transition skips validation.",
            "ruleName": None,
            "createdAt": "2026-09-25T00:00:00Z",
        },
    ]


def test_run_comments_return_empty_list_for_a_run_without_published_findings() -> None:
    run_id = UUID("00000000-0000-0000-0000-000000000100")
    repository = FakeCommentsRepository([])
    app.dependency_overrides[get_run_repository] = lambda: repository
    try:
        response = TestClient(app).get(f"/api/runs/{run_id}/comments")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json() == []


def test_run_comments_return_404_for_missing_run_and_422_for_invalid_id() -> None:
    repository = FakeCommentsRepository(None)
    app.dependency_overrides[get_run_repository] = lambda: repository
    try:
        client = TestClient(app)
        missing = client.get("/api/runs/00000000-0000-0000-0000-000000000100/comments")
        invalid = client.get("/api/runs/not-a-uuid/comments")
    finally:
        app.dependency_overrides.clear()

    assert missing.status_code == 404
    assert invalid.status_code == 422
