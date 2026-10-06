"""Verify serialized review API responses against UI-generated JSON Schemas."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, cast
from uuid import UUID

from jsonschema import Draft202012Validator

from app.main import app, get_run_repository
from app.modules.reviews.application.get_run import FindingView, RunReview
from app.modules.reviews.application.get_run_actions import RunAction, RunActionResponse
from app.modules.reviews.application.get_run_comments import PublishedComment
from app.modules.reviews.application.list_runs import RunCursor, RunListItem
from tests.portal_test_client import authenticated_test_client as TestClient

CONTRACTS_PATH = Path(__file__).parent / "fixtures" / "ui_zod_contracts.json"
RUN_ID = UUID("11111111-1111-4111-8111-111111111111")
# The ui commit the snapshot was generated from: regenerate after every ui contract change
# (`DMC_268_UI_DIR=<ui checkout after pnpm install> node tests/generate_ui_zod_contracts.mjs`).
UI_CONTRACT_COMMIT = "fd4b5c8a8d3709fe5a4c69005b63e7fc99e4819f"


class ContractRepository:
    async def has_run_access(self, run_id: UUID) -> bool:
        return run_id == RUN_ID

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

    async def list_runs(
        self,
        *,
        status: str | None,
        repository: str | None,
        cursor: RunCursor | None,
        limit: int,
    ) -> list[RunListItem]:
        item = await self.get_run(RUN_ID)
        assert item is not None
        return [item]

    async def get_run_review(self, run_id: UUID) -> RunReview | None:
        if run_id != RUN_ID:
            return None
        comments = await self.get_published_comments(run_id)
        assert comments is not None
        return RunReview(
            author="octocat",
            head_ref="feature",
            base_ref="main",
            findings=[
                FindingView(comment=comment, side="RIGHT", suggestion=None, confidence=0.8)
                for comment in comments
            ],
            summary={"problem": "One bug.", "done_well": "Clear names.", "effort": "small"},
            usage_calls=1,
            tokens_in=1200,
            tokens_out=300,
            cost_usd=Decimal("0.02"),
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
    assert contracts["provenance"]["commit"] == UI_CONTRACT_COMMIT
    return cast(dict[str, Any], contracts["schemas"])


def test_api_responses_validate_against_json_schema_generated_from_ui_zod() -> None:
    repository = ContractRepository()
    app.dependency_overrides[get_run_repository] = lambda: repository
    try:
        client = TestClient(app)
        # GET /api/runs/{id} returns RunDetail (api#20 D3); list items are RunSession.
        run_list_page = client.get("/api/runs")
        run_detail = client.get(f"/api/runs/{RUN_ID}")
        run_actions = client.get(f"/api/runs/{RUN_ID}/actions")
        review_comments = client.get(f"/api/runs/{RUN_ID}/comments")
    finally:
        app.dependency_overrides.clear()

    assert run_list_page.status_code == 200
    assert run_detail.status_code == 200
    assert run_actions.status_code == 200
    assert review_comments.status_code == 200

    schemas = _generated_schemas()
    Draft202012Validator(schemas["runListPage"]).validate(run_list_page.json())
    Draft202012Validator(schemas["runSession"]).validate(run_list_page.json()["items"][0])
    Draft202012Validator(schemas["runDetail"]).validate(run_detail.json())
    assert run_detail.json()["findings"]
    Draft202012Validator(schemas["findingView"]).validate(run_detail.json()["findings"][0])
    Draft202012Validator(schemas["runAction"]).validate(run_actions.json()[0])
    Draft202012Validator(schemas["reviewComment"]).validate(review_comments.json()[0])
