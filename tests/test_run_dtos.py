from datetime import UTC, datetime, timedelta, timezone
from uuid import UUID

from app.modules.reviews.api.dtos import PullRequestDto, RunSessionDto, RunUsageDto


def test_run_session_serializes_the_frontend_camel_case_contract() -> None:
    dto = RunSessionDto(
        id=UUID("00000000-0000-0000-0000-000000000001"),
        status="succeeded",
        engine="fast",
        attempt=0,
        cancel_requested=False,
        trigger="rerun",
        created_at=datetime(2026, 9, 23, 23, 59, tzinfo=UTC),
        started_at=datetime(2026, 9, 24, tzinfo=UTC),
        finished_at=None,
        error_code=None,
        model=None,
        action_count=0,
        summary_only=False,
        usage=RunUsageDto(tokens_in=3, tokens_out=5, cost_usd=0.375),
        triggered_by="octocat",
        pull_request=PullRequestDto(
            repo="org/repo",
            number=1,
            title="Title",
            url="https://example.test/pr/1",
            head_sha="a" * 40,
        ),
    )

    assert dto.model_dump(by_alias=True)["usage"] == {
        "tokensIn": 3,
        "tokensOut": 5,
        "costUsd": 0.375,
    }
    assert dto.model_dump(by_alias=True)["triggeredBy"] == "octocat"
    assert dto.model_dump(by_alias=True)["cancelRequested"] is False
    assert dto.model_dump(by_alias=True)["summaryOnly"] is False
    assert dto.model_dump(by_alias=True, mode="json")["trigger"] == "rerun"
    assert dto.model_dump(by_alias=True, mode="json")["createdAt"] == "2026-09-23T23:59:00Z"
    assert dto.model_dump(by_alias=True)["pullRequest"]["headSha"] == "a" * 40


def test_run_session_serializes_created_at_in_utc_whatever_its_zone() -> None:
    plus_two = timezone(timedelta(hours=2))
    dto = RunSessionDto(
        id=UUID("00000000-0000-0000-0000-000000000001"),
        status="queued",
        engine="fast",
        attempt=0,
        cancel_requested=False,
        trigger="webhook",
        created_at=datetime(2026, 9, 25, 1, 59, 30, tzinfo=plus_two),
        started_at=None,
        finished_at=None,
        error_code=None,
        model=None,
        action_count=0,
        summary_only=False,
        usage=RunUsageDto(tokens_in=0, tokens_out=0, cost_usd=0.0),
        triggered_by=None,
        pull_request=PullRequestDto(
            repo="org/repo",
            number=1,
            title="Title",
            url="https://example.test/pr/1",
            head_sha="a" * 40,
        ),
    )

    assert dto.model_dump(by_alias=True, mode="json")["createdAt"] == "2026-09-24T23:59:30Z"
    assert dto.model_dump(by_alias=True)["triggeredBy"] is None
    assert "triggeredBy" in dto.model_dump(by_alias=True)
