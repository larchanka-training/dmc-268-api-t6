"""Verify serialized review API responses against UI-generated JSON Schemas."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from uuid import UUID

from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator

from app.main import app, get_run_repository
from app.modules.reviews.application.get_run_actions import RunAction, RunActionResponse
from app.modules.reviews.application.get_run_comments import PublishedComment
from app.modules.reviews.application.list_runs import RunListItem

CONTRACTS_PATH = Path(__file__).parent / "fixtures" / "ui_zod_contracts.json"
RUN_ID = UUID("11111111-1111-4111-8111-111111111111")


class ContractRepository:
    async def get_run(self, run_id: UUID) -> RunListItem | None:
        if run_id != RUN_ID:
            return None
        return RunListItem(
            id=RUN_ID,
            status="succeeded",
            engine="fast",
            attempt=0,
            cancel_requested=False,
            started_at=datetime(2026, 9, 25, tzinfo=UTC),
            finished_at=datetime(2026, 9, 25, 0, 1, tzinfo=UTC),
            error_code=None,
            model="gpt-test",
            action_count=1,
            repo="org/repo",
            number=1,
            title="Contract fixture",
            url="https://example.test/pr/1",
            head_sha="a" * 40,
            created_at=datetime(2026, 9, 25, tzinfo=UTC),
            summary_only=False,
        )

    async def get_run_actions(self, run_id: UUID) -> list[RunAction] | None:
        if run_id != RUN_ID:
            return None
        return [
            RunAction(
                id=UUID("22222222-2222-4222-8222-222222222222"),
                run_id=RUN_ID,
                index=0,
                tool="review",
                request={"prompt": "review"},
                response={"ok": True},
                response_ref=None,
                started_at=datetime(2026, 9, 25, tzinfo=UTC),
                duration_ms=10,
            )
        ]

    async def get_run_action_response(self, run_id: UUID, index: int) -> RunActionResponse | None:
        return None

    async def get_published_comments(self, run_id: UUID) -> list[PublishedComment] | None:
        if run_id != RUN_ID:
            return None
        return [
            PublishedComment(
                id=UUID("33333333-3333-4333-8333-333333333333"),
                file="app/service.py",
                old_line=None,
                new_line=23,
                end_line=25,
                severity="high",
                category="correctness",
                title="Contract fixture",
                body="The API response must follow the UI contract.",
                rule_name="contract",
                created_at=datetime(2026, 9, 25, tzinfo=UTC),
            )
        ]


def _generated_schemas() -> dict[str, Any]:
    contracts = json.loads(CONTRACTS_PATH.read_text())
    assert contracts["provenance"]["commit"] == "68c85e0219d92459100323291921be4e1dee3d46"
    return cast(dict[str, Any], contracts["schemas"])


def test_api_responses_validate_against_json_schema_generated_from_ui_zod() -> None:
    repository = ContractRepository()
    app.dependency_overrides[get_run_repository] = lambda: repository
    try:
        client = TestClient(app)
        run_session = client.get(f"/api/runs/{RUN_ID}")
        run_actions = client.get(f"/api/runs/{RUN_ID}/actions")
        review_comments = client.get(f"/api/runs/{RUN_ID}/comments")
    finally:
        app.dependency_overrides.clear()

    assert run_session.status_code == 200
    assert run_actions.status_code == 200
    assert review_comments.status_code == 200

    schemas = _generated_schemas()
    Draft202012Validator(schemas["runSession"]).validate(run_session.json())
    Draft202012Validator(schemas["runAction"]).validate(run_actions.json()[0])
    Draft202012Validator(schemas["reviewComment"]).validate(review_comments.json()[0])
